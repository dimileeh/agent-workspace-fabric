"""Validation provenance API contract tests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

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
    from awf.db.repositories import TaskRepository

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
async def test_validation_provenance_next_cursor_fetches_second_page(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    workspace_id = await _create_v1_workspace(client)
    await _insert_validation_run(
        engine,
        run_id="vr_page_000000000001",
        workspace_id=workspace_id,
        commands=[
            {
                "phase": "setup",
                "command_index": 1,
                "command": "uv sync",
                "stream_ids": {},
            }
        ],
        started_at=datetime(2026, 4, 26, 13, 0, tzinfo=UTC),
    )
    await _insert_validation_run(
        engine,
        run_id="vr_page_000000000002",
        workspace_id=workspace_id,
        commands=[
            {
                "phase": "validate",
                "command_index": 1,
                "command": "pytest -q",
                "stream_ids": {},
            }
        ],
        started_at=datetime(2026, 4, 26, 13, 1, tzinfo=UTC),
    )

    first_response = await client.get(
        f"/v1/workspaces/{workspace_id}/validation",
        params={"limit": 1},
        headers=_AUTH_HEADERS,
    )

    assert first_response.status_code == 200
    first_page = first_response.json()
    assert [item["validation_run_id"] for item in first_page["items"]] == ["vr_page_000000000001"]
    assert first_page["has_more"] is True
    assert first_page["next_cursor"] is not None

    second_response = await client.get(
        f"/v1/workspaces/{workspace_id}/validation",
        params={"limit": 1, "cursor": first_page["next_cursor"]},
        headers=_AUTH_HEADERS,
    )

    assert second_response.status_code == 200
    second_page = second_response.json()
    assert [item["validation_run_id"] for item in second_page["items"]] == ["vr_page_000000000002"]
    assert second_page["has_more"] is False
    assert second_page["next_cursor"] is None
    assert second_page["cursor"] == first_page["next_cursor"]


@pytest.mark.unit
async def test_validation_provenance_paginates_persisted_items_before_building_responses(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _create_v1_workspace(client)
    for index, command in enumerate(("uv sync", "pytest -q", "ruff check"), start=1):
        await _insert_validation_run(
            engine,
            run_id=f"vr_build_window_{index:06d}",
            workspace_id=workspace_id,
            commands=[
                {
                    "phase": "validate",
                    "command_index": index,
                    "command": command,
                    "stream_ids": {},
                }
            ],
            started_at=datetime(2026, 4, 26, 13, index, tzinfo=UTC),
        )
    built_items = _track_validation_item_response_builds(monkeypatch)

    async with make_session_factory(engine)() as session:
        response = await validation_service.list_validation_provenance_response(
            session,
            workspace_id=workspace_id,
            limit=1,
        )

    assert response is not None
    assert [item.validation_run_id for item in response.items] == ["vr_build_window_000001"]
    assert response.has_more is True
    assert response.next_cursor is not None
    assert [item["validation_run_id"] for item in built_items] == ["vr_build_window_000001"]


@pytest.mark.unit
async def test_validation_provenance_paginates_stream_items_before_building_responses(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _create_v1_workspace_with_commands(
        client,
        ["uv sync", "pytest -q", "ruff check"],
    )
    for index in range(1, 4):
        await _create_stream_pair(
            engine,
            workspace_id=workspace_id,
            base_stream_id=f"validation.cmd_{index:02d}",
            phase="validate",
            stdout_bytes=20,
            stdout_lines=1,
            stderr_bytes=0,
            stderr_lines=0,
        )
    built_items = _track_validation_item_response_builds(monkeypatch)

    async with make_session_factory(engine)() as session:
        response = await validation_service.list_validation_provenance_response(
            session,
            workspace_id=workspace_id,
            limit=1,
        )

    assert response is not None
    assert [(item.command_index, item.command) for item in response.items] == [(1, "uv sync")]
    assert response.has_more is True
    assert response.next_cursor is not None
    assert [(item["command_index"], item["command"]) for item in built_items] == [(1, "uv sync")]


@pytest.mark.unit
def test_current_target_head_sha_skips_newer_candidates_without_head_sha() -> None:
    newer_without_head = type(
        "Candidate",
        (),
        {
            "updated_at": datetime(2026, 4, 26, 13, 0, tzinfo=UTC),
            "id": "mc_newer",
            "head_sha": None,
        },
    )()
    older_with_head = type(
        "Candidate",
        (),
        {
            "updated_at": datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
            "id": "mc_older",
            "head_sha": "older-head",
        },
    )()
    workspace = type(
        "Workspace",
        (),
        {
            "merge_candidates": [newer_without_head, older_with_head],
            "monitor_last_commit_sha": "monitor-head",
        },
    )()

    assert validation_service._current_target_head_sha(workspace) == "older-head"  # type: ignore[arg-type]


@pytest.mark.unit
def test_stream_pair_add_ignores_unknown_file_descriptors() -> None:
    stream = type("Stream", (), {})()
    pair = validation_service._StreamPair(base_stream_id="validation.cmd_01")

    pair.add("stdin", stream)  # type: ignore[arg-type]

    assert pair.streams() == []
