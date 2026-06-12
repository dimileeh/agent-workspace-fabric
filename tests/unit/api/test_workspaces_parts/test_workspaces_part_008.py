"""Workspace API contract tests.

Split out of ``test_workspaces_part_002`` to keep each part file under the
first-party line limit (``test_first_party_code_files_stay_under_line_limit``).
Covers durable idempotency-replay paths for ``create_workspace`` that must
return an existing workspace (or a 409) without creating a new row.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse

import awf.api.request_admission as request_admission
import awf.api.routes.workspaces as workspaces_route
from awf.api.schemas import (
    WorkspaceCreateRequest,
)
from awf.common.config import Settings, get_settings
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    WorkspaceRepository,
)
from awf.service.disk import DiskCheck

pytestmark = pytest.mark.usefixtures("mock_docker_cli_probe")
_MINIMAL_BODY = {
    "repo_url": "git@github.com:dimileeh/aira-agent.git",
    "branch_base": "development",
    "task_title": "Add module docstring",
    "task_prompt": "Add a one-line docstring to src/aira_agent/api/main.py.",
    "agent": "codex",
    "test_commands": ["pytest -q"],
}
_V2_MINIMAL_BODY = {
    "repo": {
        "url": "git@github.com:dimileeh/aira-agent.git",
        "base_branch": "development",
    },
    "task": {
        "title": "Add module docstring",
        "prompt": "Add a one-line docstring to src/aira_agent/api/main.py.",
        "agent": "codex",
        "kind": "feature_branch_pr",
    },
    "workspace": {"profile_ref": "auto", "profile": None},
    "validation": {"commands": ["pytest -q"], "requested_tier": 1},
    "resources": {},
}
_WORKSPACE_API_TOKEN = "unit-test-workspace-api-token"
_STABLE_REQUEST_ADMISSION_CLOCK = 1000.0


def _install_stable_request_admission_limiter(state: object) -> None:
    setattr(
        state,
        request_admission._LIMITER_STATE_KEY,  # noqa: SLF001
        request_admission.RequestAdmissionLimiter(clock=lambda: _STABLE_REQUEST_ADMISSION_CLOCK),
    )


@pytest.fixture(autouse=True)
def _provider_auth_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CODEX_AUTH_TOKEN", "unit-test-provider-token")
    monkeypatch.setenv("AWF_API_TOKEN", _WORKSPACE_API_TOKEN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _v2_body(*, title: str = "Owned path policy test") -> dict[str, object]:
    return {
        **_V2_MINIMAL_BODY,
        "task": {
            **_V2_MINIMAL_BODY["task"],
            "title": title,
        },
    }


def _disk_check(
    *,
    free_bytes: int,
    threshold_bytes: int,
    ok: bool,
) -> DiskCheck:
    return DiskCheck(
        path="/workspace/.awf",
        checked_path="/workspace",
        total_bytes=1000,
        used_bytes=1000 - free_bytes,
        free_bytes=free_bytes,
        percent_free=free_bytes / 10,
        threshold_bytes=threshold_bytes,
        ok=ok,
        status="ok" if ok else "fail",
        reason="SUFFICIENT_DISK" if ok else "INSUFFICIENT_DISK",
        detail=None if ok else "Free disk is below AWF_MIN_FREE_DISK_BYTES.",
    )


def _request_with_disk_check() -> SimpleNamespace:
    state = SimpleNamespace(
        workspace_admission_disk_check=lambda settings: _disk_check(
            free_bytes=settings.min_free_disk_bytes + 1,
            threshold_bytes=settings.min_free_disk_bytes,
            ok=True,
        )
    )
    _install_stable_request_admission_limiter(state)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _workspace_request_admission_settings(*, limit: int = 1) -> Settings:
    return Settings(
        _env_file=None,
        api_token=_WORKSPACE_API_TOKEN,
        request_admission_window_seconds=60,
        workspace_create_rate_limit_count=limit,
        callback_register_rate_limit_count=20,
    )


class TestCreateWorkspacePart002:
    @pytest.mark.unit
    async def test_v1_cache_hash_conflict_uses_durable_replay_before_conflict(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        request = _request_with_disk_check()
        payload = WorkspaceCreateRequest.model_validate(
            {**_MINIMAL_BODY, "task_title": "durable replay after stale cache hash"}
        )
        idempotency_key = "workspace-v1-stale-cache-hash"
        replay_key_cache = workspaces_route._workspace_create_idempotency_replay_key_cache(  # noqa: SLF001
            request
        )
        replay_key_cache.remember_hash(
            idempotency_key=idempotency_key,
            request_hash="stale-cache-hash",
        )
        existing = SimpleNamespace(
            id="ws_v1_stale_hash_replay",
            status=WorkspaceStatus.requested.value,
            version=3,
            created_at=datetime(2026, 5, 15, tzinfo=UTC),
            repo_url=payload.repo_url,
            branch_base=payload.branch_base,
            task_tag=payload.task.task_tag,
            task_title=payload.task_title,
            task_prompt=payload.task_prompt,
            task_external_id=payload.task_external_id,
            agent=payload.agent.value,
            env_profile=payload.env_profile,
            test_commands=list(payload.test_commands),
            requires_database=payload.requires_database,
            task_attempt=None,
        )
        lock_keys: list[str] = []
        lookup_keys: list[str] = []
        create_calls: list[str | None] = []

        async def tracked_lock(_self: WorkspaceRepository, key: str) -> None:
            lock_keys.append(key)

        async def tracked_lookup(_self: WorkspaceRepository, key: str) -> object:
            lookup_keys.append(key)
            return existing

        async def fail_create(_self: WorkspaceRepository, **kwargs: object) -> None:
            create_calls.append(kwargs.get("idempotency_key"))
            raise AssertionError("durable replay must not create a new workspace")

        monkeypatch.setattr(WorkspaceRepository, "acquire_idempotency_key_lock", tracked_lock)
        monkeypatch.setattr(WorkspaceRepository, "get_by_idempotency_key", tracked_lookup)
        monkeypatch.setattr(WorkspaceRepository, "create", fail_create)

        response = await workspaces_route.create_workspace(
            payload,
            request=request,  # type: ignore[arg-type]
            idempotency_key=idempotency_key,
            settings=_workspace_request_admission_settings(limit=10),
            session=SimpleNamespace(info={}, bind=None),  # type: ignore[arg-type]
        )

        assert not isinstance(response, JSONResponse)
        assert response.workspace_id == existing.id
        assert lock_keys == [idempotency_key]
        assert lookup_keys == [idempotency_key]
        assert create_calls == []

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("payload", "idempotency_key"),
        [
            pytest.param(
                WorkspaceCreateRequest.model_validate(
                    {**_MINIMAL_BODY, "task_title": "known missing replay v1"}
                ),
                "known-missing-workspace-v1",
                id="v1",
            ),
            pytest.param(
                WorkspaceCreateRequest.model_validate(_v2_body(title="known missing replay v2")),
                "known-missing-workspace-v2",
                id="v2",
            ),
        ],
    )
    async def test_known_replay_key_db_miss_returns_conflict_without_create(
        self,
        monkeypatch: pytest.MonkeyPatch,
        payload: WorkspaceCreateRequest,
        idempotency_key: str,
    ) -> None:
        request = _request_with_disk_check()
        replay_key_cache = workspaces_route._workspace_create_idempotency_replay_key_cache(  # noqa: SLF001
            request
        )
        replay_key_cache.remember(
            payload,
            idempotency_key=idempotency_key,
            api_version=workspaces_route._WORKSPACE_CREATE_API_VERSION,  # noqa: SLF001
        )
        lock_keys: list[str] = []
        lookup_keys: list[str] = []
        create_calls: list[str | None] = []

        async def tracked_lock(_self: WorkspaceRepository, key: str) -> None:
            lock_keys.append(key)

        async def tracked_lookup(_self: WorkspaceRepository, key: str) -> None:
            lookup_keys.append(key)

        async def fail_create(*_args: object, **kwargs: object) -> None:
            create_calls.append(kwargs.get("idempotency_key"))
            raise AssertionError("known replay-key durable miss must not create a workspace")

        monkeypatch.setattr(WorkspaceRepository, "acquire_idempotency_key_lock", tracked_lock)
        monkeypatch.setattr(WorkspaceRepository, "get_by_idempotency_key", tracked_lookup)
        monkeypatch.setattr(workspaces_route, "create_workspace_row_checked", fail_create)

        session = SimpleNamespace(info={}, bind=None)
        response = await workspaces_route.create_workspace(
            payload,
            request=request,  # type: ignore[arg-type]
            idempotency_key=idempotency_key,
            settings=_workspace_request_admission_settings(limit=10),
            session=session,  # type: ignore[arg-type]
        )

        assert isinstance(response, JSONResponse)
        assert response.status_code == 409
        assert json.loads(response.body)["error_code"] == "IDEMPOTENCY_REPLAY_UNAVAILABLE"
        assert lock_keys == [idempotency_key]
        assert lookup_keys == [idempotency_key]
        assert create_calls == []
