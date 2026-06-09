"""Validation provenance API contract tests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

import awf.api.routes.validation as validation_route
import awf.service.validation_provenance as validation_service
from awf.common.config import get_settings
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.repositories import (
    MergeCandidateRepository,
    TaskAttemptRepository,
    WorkspaceLogStreamRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory

pytestmark = pytest.mark.usefixtures("mock_docker_cli_probe")


@pytest.fixture(autouse=True)
def _provider_auth_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWF_API_TOKEN", "secret")
    monkeypatch.setenv("CODEX_AUTH_TOKEN", "unit-test-provider-token")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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
_AUTH_HEADERS = {"Authorization": "Bearer secret"}


async def _create_profile_workspace(client: AsyncClient) -> str:
    response = await client.post("/v1/workspaces", json=_V2_PROFILE_BODY, headers=_AUTH_HEADERS)
    assert response.status_code == 202
    return str(response.json()["workspace_id"])


async def _create_workspace_with_body(client: AsyncClient, body: dict) -> str:
    response = await client.post("/v1/workspaces", json=body, headers=_AUTH_HEADERS)
    assert response.status_code == 202
    return str(response.json()["workspace_id"])


async def _create_v1_workspace(client: AsyncClient) -> str:
    response = await client.post("/v1/workspaces", json=_V1_BODY, headers=_AUTH_HEADERS)
    assert response.status_code == 202
    return str(response.json()["workspace_id"])


async def _create_v1_workspace_with_commands(
    client: AsyncClient,
    commands: list[str],
) -> str:
    response = await client.post(
        "/v1/workspaces",
        json={**_V1_BODY, "test_commands": commands},
        headers=_AUTH_HEADERS,
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


async def _create_validation_stream(
    engine: AsyncEngine,
    *,
    workspace_id: str,
    stream_id: str,
    kind: str,
    name: str,
    byte_count: int = 1,
    line_count: int = 1,
    closed: bool = True,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        repo = WorkspaceLogStreamRepository(session)
        stream = await repo.create_or_get(
            workspace_id=workspace_id,
            stream_id=stream_id,
            source="validation",
            name=name,
            kind=kind,
            path=f"/tmp/{stream_id}.log",
        )
        stream.opened_at = datetime(2026, 4, 26, 14, 0, tzinfo=UTC)
        await repo.append_metadata(
            workspace_id=workspace_id,
            stream_id=stream_id,
            byte_delta=byte_count,
            line_delta=line_count,
        )
        if closed:
            await repo.close(workspace_id=workspace_id, stream_id=stream_id)
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


async def _clear_workspace_failure_message(engine: AsyncEngine, workspace_id: str) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.status = WorkspaceStatus.failed.value
        workspace.failure_reason = FailureReason.validation_failure.value
        workspace.failure_message = None
        await session.commit()


async def _store_resolved_profile(engine: AsyncEngine, workspace_id: str, profile: dict) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.resolved_profile = profile
        await session.commit()


async def _attach_merge_candidate(
    engine: AsyncEngine,
    workspace_id: str,
    *,
    head_sha: str | None,
    updated_at: datetime,
) -> None:
    from awf.db.repositories import MergeCandidateRepository, TaskAttemptRepository, TaskRepository

    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.pr_url = "https://github.com/example/provenance/pull/1"
        workspace.pr_number = 1
        workspace.branch_name = "codex/validation-provenance"
        task_repo = TaskRepository(session)
        attempt_repo = TaskAttemptRepository(session)
        attempt = await attempt_repo.get_by_workspace_id(workspace.id)
        if attempt is None:
            task = await task_repo.create_or_get(
                repo_url=workspace.repo_url,
                base_branch=workspace.branch_base,
                title=workspace.task_title,
                prompt=workspace.task_prompt,
                external_id=f"VALIDATION-{head_sha or 'missing'}",
                idempotency_key=None,
                task_class=workspace.task_class,
                owned_paths=list(workspace.owned_paths),
            )
            attempt = await attempt_repo.create_for_workspace(
                task=task,
                workspace=workspace,
            )
        else:
            task = await task_repo.get(attempt.task_id)
            assert task is not None
        attempt.is_canonical_for_merge = True
        candidate = await MergeCandidateRepository(session).create_or_update_open_for_attempt(
            task=task,
            attempt=attempt,
            workspace=workspace,
            head_sha=head_sha,
            base_sha="base-head",
        )
        candidate.updated_at = updated_at
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
    commands: object | None = None,
    base_commit: str | None = "base-persisted",
    base_sha: str | None = None,
    workspace_head_sha: str | None = None,
    target_branch: str | None = "awf/persisted-validation",
    target_head_sha: str | None = "target-persisted",
    profile_name: str | None = None,
    profile_version: int | None = None,
    profile_source: str | None = None,
    resolved_profile_digest: str | None = None,
    environment_identity_digest: str | None = None,
    environment_identity_inputs: dict | None = None,
    status: str = "succeeded",
    reason_code: str | None = "VALIDATION_OK",
    started_at: datetime = datetime(2026, 4, 26, 13, 0, tzinfo=UTC),
    finished_at: datetime | None = datetime(2026, 4, 26, 13, 2, tzinfo=UTC),
    log_stream_refs: dict | None = None,
    retry_count: int = 0,
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
                    base_sha,
                    workspace_head_sha,
                    target_branch,
                    target_head_sha,
                    profile_name,
                    profile_version,
                    profile_source,
                    resolved_profile_digest,
                    environment_identity_digest,
                    environment_identity_inputs,
                    status,
                    reason_code,
                    retry_count,
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
                    :base_sha,
                    :workspace_head_sha,
                    :target_branch,
                    :target_head_sha,
                    :profile_name,
                    :profile_version,
                    :profile_source,
                    :resolved_profile_digest,
                    :environment_identity_digest,
                    :environment_identity_inputs,
                    :status,
                    :reason_code,
                    :retry_count,
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
                "base_sha": base_sha,
                "workspace_head_sha": workspace_head_sha,
                "target_branch": target_branch,
                "target_head_sha": target_head_sha,
                "profile_name": profile_name,
                "profile_version": profile_version,
                "profile_source": profile_source,
                "resolved_profile_digest": resolved_profile_digest,
                "environment_identity_digest": environment_identity_digest,
                "environment_identity_inputs": json.dumps(environment_identity_inputs)
                if environment_identity_inputs is not None
                else None,
                "status": status,
                "reason_code": reason_code,
                "retry_count": retry_count,
                "started_at": started_at,
                "finished_at": finished_at,
                "log_stream_refs": json.dumps(log_stream_refs or {}),
            },
        )
        await session.commit()


def _track_validation_item_response_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, object]]:
    built_items: list[dict[str, object]] = []
    original_response = validation_service.ValidationProvenanceItemResponse

    def counting_response(**kwargs: object) -> object:
        built_items.append(dict(kwargs))
        return original_response(**kwargs)

    monkeypatch.setattr(
        validation_service,
        "ValidationProvenanceItemResponse",
        counting_response,
    )
    return built_items


@pytest.mark.unit
def test_validation_provenance_ensure_utc_accepts_naive_datetime() -> None:
    naive = datetime(2026, 5, 7, 13, 45)

    assert validation_service._ensure_utc(naive) == naive.replace(tzinfo=UTC)


def test_validation_route_exports_only_route_endpoint() -> None:
    assert validation_route.__all__ == ["list_validation_provenance"]


@pytest.mark.unit
async def test_validation_route_maps_invalid_cursor_to_structured_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise_invalid_cursor(*_args: object, **_kwargs: object) -> object:
        raise validation_route.InvalidBoundedListCursorError("bad cursor")

    monkeypatch.setattr(
        validation_route,
        "list_validation_provenance_response",
        _raise_invalid_cursor,
    )

    with pytest.raises(HTTPException) as exc_info:
        await validation_route.list_validation_provenance(
            "ws_test",
            cursor="bad",
            session=object(),  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == {
        "error_code": "INVALID_CURSOR",
        "message": "Invalid validation provenance cursor.",
    }


@pytest.mark.unit
def test_validation_provenance_command_lookup_includes_database_hooks() -> None:
    profile = {
        "name": "api-provenance-db-hooks",
        "database": {
            "generated_setup": ["python scripts/db_generated_setup.py"],
            "pre_validation_refresh": ["python scripts/db_refresh.py"],
        },
    }

    lookup = validation_service._command_lookup(
        SimpleNamespace(resolved_profile=profile, test_commands=[])
    )

    assert lookup[("db_generated_setup", 1)] == "python scripts/db_generated_setup.py"
    assert lookup[("db_refresh", 1)] == "python scripts/db_refresh.py"


@pytest.mark.unit
async def test_attach_merge_candidate_reuses_existing_attempt_task(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_profile_workspace(client)

    await _attach_merge_candidate(
        engine,
        workspace_id,
        head_sha="helper-regression-head",
        updated_at=datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
    )

    factory = make_session_factory(engine)
    async with factory() as session:
        attempt = await TaskAttemptRepository(session).get_by_workspace_id(workspace_id)
        assert attempt is not None
        candidate = await MergeCandidateRepository(session).get_by_attempt_id(attempt.id)
        assert candidate is not None
        assert candidate.task_id == attempt.task_id


@pytest.mark.unit
async def test_validation_provenance_groups_streams_and_resolves_profile_commands(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_profile_workspace(client)
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

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation", headers=_AUTH_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] is None
    assert body["has_more"] is False
    assert body["limit"] == validation_service.DEFAULT_VALIDATION_PROVENANCE_LIMIT
    assert body["cursor"] is None
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

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation", headers=_AUTH_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["limit"] == validation_service.DEFAULT_VALIDATION_PROVENANCE_LIMIT
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
    assert item["retry_count"] == 0
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
async def test_validation_provenance_exposes_persisted_identity_fields(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_v1_workspace(client)
    await _attach_merge_candidate(
        engine,
        workspace_id,
        head_sha=None,
        updated_at=datetime(2026, 4, 26, 13, 0, tzinfo=UTC),
    )
    identity_inputs = {
        "schema_version": 1,
        "runtime": {
            "agent_image": "ghcr.io/acme/agent:1",
            "toolchain_image": "ghcr.io/acme/toolchain:1",
        },
    }
    await _insert_validation_run(
        engine,
        run_id="vr_identity_fields_000001",
        workspace_id=workspace_id,
        base_commit="legacy-base",
        base_sha="base-identity",
        workspace_head_sha="workspace-head-identity",
        target_branch="development",
        target_head_sha="workspace-head-identity",
        profile_name="python",
        profile_version=12,
        profile_source="repo:.awf/workspace.yml",
        resolved_profile_digest="1" * 64,
        environment_identity_digest="2" * 64,
        environment_identity_inputs=identity_inputs,
    )

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation", headers=_AUTH_HEADERS)

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["base_commit"] == "legacy-base"
    assert item["base_sha"] == "base-identity"
    assert item["workspace_head_sha"] == "workspace-head-identity"
    assert item["profile_name"] == "python"
    assert item["profile_version"] == 12
    assert item["profile_source"] == "repo:.awf/workspace.yml"
    assert item["resolved_profile_digest"] == "1" * 64
    assert item["environment_identity_digest"] == "2" * 64
    assert item["environment_identity_inputs"] == identity_inputs
    assert item["identity_source"] == "persisted"
    assert item["current_target_head_sha"] is None
    assert item["fresh_for_target"] is None


@pytest.mark.unit
async def test_validation_provenance_legacy_row_uses_safe_identity_fallbacks(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_v1_workspace(client)
    await _insert_validation_run(
        engine,
        run_id="vr_legacy_identity_000001",
        workspace_id=workspace_id,
        base_commit="legacy-base-only",
        target_head_sha="legacy-target-head",
    )

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation", headers=_AUTH_HEADERS)

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["base_commit"] == "legacy-base-only"
    assert item["base_sha"] == "legacy-base-only"
    assert item["workspace_head_sha"] == "legacy-target-head"
    assert item["profile_name"] is None
    assert item["profile_version"] is None
    assert item["profile_source"] is None
    assert item["resolved_profile_digest"] is None
    assert item["environment_identity_digest"] is None
    assert item["environment_identity_inputs"] == {}
    assert item["identity_source"] == "legacy_fallback"


@pytest.mark.unit
async def test_validation_provenance_reports_persisted_coverage_policy(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_v1_workspace(client)
    await _mark_workspace_validation_failed(
        engine,
        workspace_id,
        message="validation failed: coverage 98.4% is below required 99.0%",
    )
    await _insert_validation_run(
        engine,
        run_id="vr_222222222222222222222222",
        workspace_id=workspace_id,
        status="failed",
        reason_code="COVERAGE_BELOW_THRESHOLD",
        log_stream_refs={
            "coverage": {
                "provider": "python",
                "percent": 98.4,
                "minimum_percent": 99.0,
                "enforce": True,
                "status": "failed",
                "reason_code": "COVERAGE_BELOW_THRESHOLD",
            }
        },
    )

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation", headers=_AUTH_HEADERS)

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["coverage_percent"] == 98.4
    assert item["coverage_minimum_percent"] == 99.0
    assert item["coverage_status"] == "failed"
    assert item["coverage_reason_code"] == "COVERAGE_BELOW_THRESHOLD"


@pytest.mark.unit
async def test_validation_provenance_reports_failing_test_evidence(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_v1_workspace(client)
    await _insert_validation_run(
        engine,
        run_id="vr_failing_tests_0000000001",
        workspace_id=workspace_id,
        status="failed",
        reason_code="PYTEST_TEST_FAILURE",
        log_stream_refs={
            "coverage": {
                "provider": "python",
                "percent": 99.2,
                "minimum_percent": 99.0,
                "enforce": True,
                "status": "passed",
                "reason_code": "COVERAGE_OK",
                "failing_test_node_ids": [
                    "tests/unit/test_widget.py::test_handles_edges",
                ],
                "failing_test_evidence": [
                    "FAILED tests/unit/test_widget.py::test_handles_edges - AssertionError",
                ],
            }
        },
    )

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation", headers=_AUTH_HEADERS)

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["coverage_percent"] == 99.2
    assert item["coverage_status"] == "passed"
    assert item["coverage_reason_code"] == "COVERAGE_OK"
    assert item["failing_test_node_ids"] == [
        "tests/unit/test_widget.py::test_handles_edges",
    ]
    assert item["failing_test_evidence"] == [
        "FAILED tests/unit/test_widget.py::test_handles_edges - AssertionError",
    ]


@pytest.mark.unit
async def test_validation_provenance_malformed_persisted_command_uses_safe_defaults(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_v1_workspace(client)
    await _insert_validation_run(
        engine,
        run_id="vr_malformed_command_000001",
        workspace_id=workspace_id,
        commands=[
            {
                "phase": 123,
                "command_index": "first",
                "command": ["pytest"],
                "stream_ids": "not-a-dict",
            }
        ],
        target_branch=None,
        target_head_sha=None,
    )

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation", headers=_AUTH_HEADERS)

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["phase"] == "unknown"
    assert item["command_index"] == 0
    assert item["command"] is None
    assert item["stream_ids"] == {"stdout": None, "stderr": None}
    assert item["stdout_byte_count"] == 0
    assert item["stderr_byte_count"] == 0
    assert item["target_branch"] is None
    assert item["branch_name"] is None


@pytest.mark.unit
async def test_validation_provenance_malformed_persisted_commands_container_uses_fallback(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_v1_workspace(client)
    await _insert_validation_run(
        engine,
        run_id="vr_malformed_commands_00001",
        workspace_id=workspace_id,
        commands={"unexpected": "object"},
    )

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation", headers=_AUTH_HEADERS)

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["phase"] == "unknown"
    assert item["command_index"] == 0
    assert item["command"] is None
    assert item["stream_ids"] == {"stdout": None, "stderr": None}


@pytest.mark.unit
async def test_validation_provenance_uses_latest_candidate_head_for_freshness(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_v1_workspace(client)
    await _attach_merge_candidate(
        engine,
        workspace_id,
        head_sha="candidate-head",
        updated_at=datetime(2026, 4, 26, 13, 0, tzinfo=UTC),
    )
    await _insert_validation_run(
        engine,
        run_id="vr_candidate_head_0000001",
        workspace_id=workspace_id,
        target_head_sha="run-head",
    )

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation", headers=_AUTH_HEADERS)

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["target_head_sha"] == "run-head"
    assert item["current_target_head_sha"] == "candidate-head"
    assert item["fresh_for_target"] is False


@pytest.mark.unit
async def test_validation_provenance_resolves_profile_commands_by_phase_index(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_workspace_with_body(
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

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation", headers=_AUTH_HEADERS)

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
async def test_validation_provenance_displays_http_healthcheck_target(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_workspace_with_body(
        client,
        {
            **_V2_PROFILE_BODY,
            "workspace": {
                "profile_ref": "auto",
                "profile": {
                    "name": "api-provenance-http-healthcheck-test",
                    "phases": {"validate": ["pytest -q"]},
                    "validation": {
                        "healthchecks": [
                            {
                                "name": "api",
                                "url": "http://api:8080/healthz",
                                "expected_status": 204,
                            }
                        ]
                    },
                },
            },
            "validation": {"commands": [], "requested_tier": 1},
        },
    )
    await _create_stream_pair(
        engine,
        workspace_id=workspace_id,
        base_stream_id="validation.01_healthcheck",
        phase="healthcheck",
        stdout_bytes=1,
        stdout_lines=1,
        stderr_bytes=0,
        stderr_lines=0,
    )

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation", headers=_AUTH_HEADERS)

    assert response.status_code == 200
    assert [
        (item["phase"], item["command_index"], item["command"]) for item in response.json()["items"]
    ] == [("healthcheck", 1, "GET http://api:8080/healthz expected 204")]


@pytest.mark.unit
async def test_validation_provenance_resolves_coverage_command_from_profile(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_workspace_with_body(
        client,
        {
            **_V2_PROFILE_BODY,
            "workspace": {
                "profile_ref": "auto",
                "profile": {
                    "name": "api-provenance-coverage-command-test",
                    "phases": {"validate": ["pytest -q"]},
                    "validation": {
                        "coverage": {
                            "minimum_percent": 99,
                            "command": "uv run pytest --cov=awf --cov-report=term",
                        }
                    },
                },
            },
        },
    )
    await _create_stream_pair(
        engine,
        workspace_id=workspace_id,
        base_stream_id="validation.01_coverage",
        phase="coverage",
        stdout_bytes=10,
        stdout_lines=1,
        stderr_bytes=0,
        stderr_lines=0,
    )

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation", headers=_AUTH_HEADERS)

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["phase"] == "coverage"
    assert item["command_index"] == 1
    assert item["command"] == "uv run pytest --cov=awf --cov-report=term"


@pytest.mark.unit
async def test_validation_provenance_handles_stream_id_suffixes_and_label_fallbacks(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_v1_workspace(client)
    await _mark_workspace_completed(engine, workspace_id)
    await _create_validation_stream(
        engine,
        workspace_id=workspace_id,
        stream_id="validation.run_42.stdout",
        kind="log",
        name="coverage run_42 stdout",
        byte_count=4,
        line_count=1,
    )
    await _create_validation_stream(
        engine,
        workspace_id=workspace_id,
        stream_id="setup.01_cleanup.stderr",
        kind="log",
        name="cleanup setup stderr",
        byte_count=7,
        line_count=2,
    )
    await _create_validation_stream(
        engine,
        workspace_id=workspace_id,
        stream_id="plainstream",
        kind="stdout",
        name="unknown plain stdout",
        byte_count=5,
        line_count=1,
    )
    await _create_validation_stream(
        engine,
        workspace_id=workspace_id,
        stream_id="ignored.log",
        kind="log",
        name="ignored log",
    )

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation", headers=_AUTH_HEADERS)

    assert response.status_code == 200
    items = response.json()["items"]
    assert [(item["phase"], item["command_index"]) for item in items] == [
        ("coverage", 42),
        ("cleanup", 1),
        ("unknown", 0),
    ]
    assert items[0]["stream_ids"] == {
        "stdout": "validation.run_42.stdout",
        "stderr": None,
    }
    assert items[1]["stream_ids"] == {
        "stdout": None,
        "stderr": "setup.01_cleanup.stderr",
    }
    assert items[2]["stream_ids"] == {"stdout": "plainstream", "stderr": None}


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

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation", headers=_AUTH_HEADERS)

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
async def test_validation_provenance_ignores_malformed_resolved_profile(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_v1_workspace_with_commands(client, ["pytest -q"])
    await _store_resolved_profile(engine, workspace_id, {"name": "", "phases": "invalid"})
    await _create_stream_pair(
        engine,
        workspace_id=workspace_id,
        base_stream_id="validation.cmd_01",
        phase="validate",
        stdout_bytes=12,
        stdout_lines=1,
        stderr_bytes=0,
        stderr_lines=0,
    )

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation", headers=_AUTH_HEADERS)

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["phase"] == "validate"
    assert item["command"] == "pytest -q"


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

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation", headers=_AUTH_HEADERS)

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

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation", headers=_AUTH_HEADERS)

    assert response.status_code == 200
    assert [(item["command"], item["status"]) for item in response.json()["items"]] == [
        ("pytest -q", "succeeded"),
        ("ruff check", "failed"),
    ]


@pytest.mark.unit
async def test_validation_provenance_keeps_records_after_failed_command_unknown(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_v1_workspace_with_commands(
        client,
        ["pytest -q", "ruff check", "mypy src"],
    )
    await _mark_workspace_validation_failed(engine, workspace_id)
    for command_index in (1, 2, 3):
        await _create_stream_pair(
            engine,
            workspace_id=workspace_id,
            base_stream_id=f"validation.cmd_0{command_index}",
            phase="validate",
            stdout_bytes=20,
            stdout_lines=1,
            stderr_bytes=0,
            stderr_lines=0,
        )

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation", headers=_AUTH_HEADERS)

    assert response.status_code == 200
    assert [(item["command"], item["status"]) for item in response.json()["items"]] == [
        ("pytest -q", "succeeded"),
        ("ruff check", "failed"),
        ("mypy src", "unknown"),
    ]


@pytest.mark.unit
async def test_validation_provenance_failed_workspace_without_message_keeps_status_unknown(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_v1_workspace_with_commands(client, ["pytest -q"])
    await _clear_workspace_failure_message(engine, workspace_id)
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

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation", headers=_AUTH_HEADERS)

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["command"] == "pytest -q"
    assert item["status"] == "unknown"


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

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation", headers=_AUTH_HEADERS)

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

    response = await client.get(f"/v1/workspaces/{workspace_id}/validation", headers=_AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "next_cursor": None,
        "has_more": False,
        "limit": validation_service.DEFAULT_VALIDATION_PROVENANCE_LIMIT,
        "cursor": None,
    }


@pytest.mark.unit
async def test_validation_provenance_missing_workspace_returns_404(
    client: AsyncClient,
) -> None:
    response = await client.get("/v1/workspaces/ws_missing/validation", headers=_AUTH_HEADERS)

    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "NOT_FOUND"


@pytest.mark.unit
async def test_validation_route_function_raises_structured_404_for_missing_workspace(
    engine: AsyncEngine,
) -> None:
    async with make_session_factory(engine)() as session:
        with pytest.raises(HTTPException) as exc_info:
            await validation_route.list_validation_provenance("ws_missing", session=session)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == {
        "error_code": "NOT_FOUND",
        "message": "No workspace with id ws_missing",
    }


@pytest.mark.unit
async def test_validation_route_function_returns_stream_derived_items(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_v1_workspace_with_commands(client, ["pytest -q"])
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

    async with make_session_factory(engine)() as session:
        response = await validation_route.list_validation_provenance(
            workspace_id,
            session=session,
        )

    assert [(item.phase, item.command_index, item.command) for item in response.items] == [
        ("validate", 1, "pytest -q")
    ]
    assert response.next_cursor is None
    assert response.has_more is False
    assert response.limit == validation_service.DEFAULT_VALIDATION_PROVENANCE_LIMIT
    assert response.cursor is None


@pytest.mark.unit
async def test_validation_route_function_returns_persisted_run_items(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_v1_workspace(client)
    await _insert_validation_run(
        engine,
        run_id="vr_direct_route_000000001",
        workspace_id=workspace_id,
        commands=[
            {
                "phase": "validate",
                "command_index": 1,
                "command": "pytest -q",
                "stream_ids": {},
            }
        ],
    )

    async with make_session_factory(engine)() as session:
        response = await validation_route.list_validation_provenance(
            workspace_id,
            session=session,
        )

    assert [(item.validation_run_id, item.phase, item.command) for item in response.items] == [
        ("vr_direct_route_000000001", "validate", "pytest -q")
    ]
