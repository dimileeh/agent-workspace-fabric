"""``POST /v1/service/gc`` route tests.

The route is the root control-plane GC trigger the thin ``awf service gc`` CLI
calls. These tests cover auth, dry-run planning, request-param mapping, and the
``partial`` envelope; the deletion mechanism itself is covered by the gc service
entrypoint unit tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory

pytestmark = pytest.mark.usefixtures("mock_docker_cli_probe")


async def _seed_completed_merged(
    engine: AsyncEngine,
    *,
    updated_at: datetime,
) -> str:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/repo.git",
            branch_base="development",
            task_title="gc candidate",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        workspace.status = WorkspaceStatus.completed.value
        workspace.updated_at = updated_at
        workspace.pr_url = "https://github.com/example/repo/pull/9"
        workspace.pr_number = 9
        workspace.pr_merge_sha = "a" * 40
        await session.commit()
        return workspace.id


async def _seed_cancelled(
    engine: AsyncEngine,
    *,
    updated_at: datetime,
) -> str:
    """Seed an aged cancelled workspace.

    The conservative default GC policy never classifies ``cancelled``/``destroyed``
    rows, so the dry-run plan omits this workspace — but ``--execute`` reaps its auth
    dir via the worker's discarded-status augmentation pass (#513).
    """
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/repo.git",
            branch_base="development",
            task_title="gc cancelled",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        workspace.status = WorkspaceStatus.cancelled.value
        workspace.updated_at = updated_at
        await session.commit()
        return workspace.id


class _FakeGCResult:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def to_dict(self) -> dict[str, object]:
        return self._payload


@pytest.mark.unit
async def test_service_gc_requires_api_token(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/service/gc",
        json={},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["error_code"] == "UNAUTHORIZED"


@pytest.mark.unit
async def test_service_gc_dry_run_returns_plan(
    client: AsyncClient,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWF_WORK_DIR", str(tmp_path / "service"))
    workspace_id = await _seed_completed_merged(
        engine,
        updated_at=datetime.now(UTC) - timedelta(hours=400),
    )

    response = await client.post(
        "/v1/service/gc",
        json={"min_age_hours": 24},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["dry_run"] is True
    assert payload["status"] == "dry_run"
    assert payload["candidate_count"] == 1
    assert payload["candidates"][0]["workspace_id"] == workspace_id


@pytest.mark.unit
async def test_service_gc_dry_run_previews_discarded_status_pass(
    client: AsyncClient,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dry-run must preview the worker's cancelled/destroyed augmentation pass (#513).

    ``--execute`` delegates to the worker's two-pass GC, whose second pass reaps the
    cancelled/destroyed auth dirs the default policy never classifies. If the dry-run
    plan omitted them an operator would see ``candidate_count: 0`` yet ``--execute``
    would delete the auth dir — breaking the plan-before-delete contract
    (PRRT_kwDOSJAM6s6JbN6x). With no explicit ``--status`` the preview now lists the
    aged cancelled workspace.
    """
    monkeypatch.setenv("AWF_WORK_DIR", str(tmp_path / "service"))
    cancelled_id = await _seed_cancelled(
        engine,
        updated_at=datetime.now(UTC) - timedelta(hours=400),
    )

    response = await client.post("/v1/service/gc", json={"min_age_hours": 24})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["dry_run"] is True
    assert payload["status"] == "dry_run"
    candidate_ids = {c["workspace_id"] for c in payload["candidates"]}
    assert cancelled_id in candidate_ids


@pytest.mark.unit
async def test_service_gc_dry_run_explicit_status_skips_augmentation(
    client: AsyncClient,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``--status`` scopes both dry-run and execute, so no augmentation runs.

    With ``--status completed`` the worker runs only the first (explicit-scope) pass,
    so the dry-run must mirror that scope and never preview the cancelled row — keeping
    plan and execute symmetric (#590).
    """
    monkeypatch.setenv("AWF_WORK_DIR", str(tmp_path / "service"))
    cancelled_id = await _seed_cancelled(
        engine,
        updated_at=datetime.now(UTC) - timedelta(hours=400),
    )

    response = await client.post(
        "/v1/service/gc",
        json={"min_age_hours": 24, "statuses": ["completed"]},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["dry_run"] is True
    candidate_ids = {c["workspace_id"] for c in payload["candidates"]}
    assert cancelled_id not in candidate_ids


@pytest.mark.unit
async def test_service_gc_dry_run_previews_claude_base_reap_for_discarded_pass(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dry-run discarded preview pass also runs the claude-base reaper (PRRT_kwDOSJAM6s6JbT1B).

    ``--execute``'s second pass now reaps shared bases unpinned once the
    cancelled/destroyed auth dirs are deleted, so the plan-before-delete preview must
    run the base reaper on the augmentation pass too — otherwise the plan would label a
    base ``protected`` that ``--execute`` reaps, breaking the plan/execute parity
    (PRRT_kwDOSJAM6s6JbN6x).
    """
    monkeypatch.setenv("AWF_WORK_DIR", str(tmp_path / "service"))
    calls: list[dict[str, object]] = []

    def _payload(candidate_count: int) -> dict[str, object]:
        return {
            "dry_run": True,
            "status": "dry_run",
            "reason_code": "CLEANUP_DRY_RUN",
            "candidate_count": candidate_count,
            "candidates": [],
            "deleted_paths": [],
            "delete_errors": [],
            "preserved_count": 0,
            "deleted_path_count": 0,
            "total_estimated_bytes": 0,
        }

    async def _fake_entrypoint(_session_factory: object, **kwargs: object) -> _FakeGCResult:
        calls.append(kwargs)
        # The first (default) pass reports a candidate so the augmentation pass runs;
        # the second pass is the one whose reap flags we assert.
        return _FakeGCResult(_payload(1 if len(calls) == 1 else 0))

    with patch(
        "awf.service.gc_request.run_service_workspace_gc",
        new=AsyncMock(side_effect=_fake_entrypoint),
    ):
        response = await client.post("/v1/service/gc", json={"min_age_hours": 24})

    assert response.status_code == 200, response.text
    assert len(calls) == 2
    discarded_call = calls[1]
    assert discarded_call["execute"] is False
    assert discarded_call["reap_claude_bases"] is True
    assert isinstance(discarded_call["host_home"], Path)


@pytest.mark.unit
async def test_service_gc_dry_run_discarded_pass_budget_exhausted_passes_limit_zero(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the first pass exhausts the shared batch the discarded pass gets ``limit=0``.

    The two preview passes share one cleanup-batch budget
    (``discarded_limit = candidate_limit - first-pass candidates``) so the dry-run can
    never list more candidates than ``--execute`` could reap in one batch
    (PRRT_kwDOSJAM6s6JcnXh). If the first (default-policy) pass already fills the limit,
    the second (cancelled/destroyed) preview must be budgeted to 0 — ``plan_terminal_workspace_gc``
    clamps that to ``.limit(0)`` and lists nothing, so the plan does not over-list.
    """
    monkeypatch.setenv("AWF_WORK_DIR", str(tmp_path / "service"))
    calls: list[dict[str, object]] = []

    def _payload(candidate_count: int) -> dict[str, object]:
        return {
            "dry_run": True,
            "status": "dry_run",
            "reason_code": "CLEANUP_DRY_RUN",
            "candidate_count": candidate_count,
            "candidates": [],
            "deleted_paths": [],
            "delete_errors": [],
            "preserved_count": 0,
            "deleted_path_count": 0,
            "total_estimated_bytes": 0,
        }

    async def _fake_entrypoint(_session_factory: object, **kwargs: object) -> _FakeGCResult:
        calls.append(kwargs)
        # First (default) pass fills the whole ``--limit 1`` batch so the discarded
        # pass's share is ``1 - 1 == 0``.
        return _FakeGCResult(_payload(1 if len(calls) == 1 else 0))

    with patch(
        "awf.service.gc_request.run_service_workspace_gc",
        new=AsyncMock(side_effect=_fake_entrypoint),
    ):
        response = await client.post(
            "/v1/service/gc",
            json={"min_age_hours": 24, "limit": 1},
        )

    assert response.status_code == 200, response.text
    assert len(calls) == 2
    assert calls[0]["limit"] == 1
    assert calls[1]["limit"] == 0


@pytest.mark.unit
async def test_service_gc_dry_run_previews_base_pinned_by_both_passes(
    client: AsyncClient,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A base pinned by a default *and* a discarded candidate previews as planned (PRRT_kwDOSJAM6s6Jbinh).

    ``--execute`` deletes the first (default-policy) pass's auth dirs before the second
    (cancelled/destroyed) pass's claude-base reaper runs, so a superseded base pinned by
    *both* a completed-merged and a cancelled workspace is reaped by the second execute
    pass. The dry-run must mirror that: previewing only the discarded pins in the second
    pass would still see the default candidate's pin on disk and mislabel the base
    ``protected`` even though ``--execute`` reaps it — breaking the plan-before-delete
    contract. The second preview pass therefore treats the first pass's planned auth dirs
    as already pruned, so the base lands in ``claude_base_reap.planned``.
    """
    from awf.node.auth_mounts import _shared_claude_base_dir

    work_dir = tmp_path / "service"
    monkeypatch.setenv("AWF_WORK_DIR", str(work_dir))
    # A host home with no ``~/.claude`` so the superseded signature is not the current
    # one (and is therefore reapable once unpinned).
    monkeypatch.setenv("AWF_HOST_HOME", str(tmp_path / "home"))
    old = datetime.now(UTC) - timedelta(hours=400)
    completed_id = await _seed_completed_merged(engine, updated_at=old)
    cancelled_id = await _seed_cancelled(engine, updated_at=old)

    resolved_work_dir = work_dir.expanduser().resolve()
    signature = "sigsuperseded000"
    base = _shared_claude_base_dir(resolved_work_dir, signature)
    base.mkdir(parents=True)
    (base / "blob").write_text("x" * 256, encoding="utf-8")
    # Both candidates pin the same superseded base. No overlay ``upper`` is created, so
    # the capability-less API reaper has no unverifiable live overlay and can plan.
    for workspace_id in (completed_id, cancelled_id):
        pin_dir = resolved_work_dir / "auth" / workspace_id / "claude"
        pin_dir.mkdir(parents=True)
        (pin_dir / "base.signature").write_text(signature, encoding="utf-8")

    response = await client.post("/v1/service/gc", json={"min_age_hours": 24})

    assert response.status_code == 200, response.text
    payload = response.json()
    reap = payload["claude_base_reap"]
    assert signature in reap["planned"]
    assert signature not in reap["protected"]


@pytest.mark.unit
async def test_service_gc_maps_request_params_to_entrypoint(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWF_WORK_DIR", str(tmp_path / "service"))
    captured: dict[str, object] = {}

    async def _fake_entrypoint(_session_factory: object, **kwargs: object) -> _FakeGCResult:
        captured.update(kwargs)
        return _FakeGCResult(
            {
                "dry_run": False,
                "status": "succeeded",
                "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
                "candidate_count": 1,
                "preserved_count": 0,
                "deleted_path_count": 3,
                "total_estimated_bytes": 42,
            }
        )

    with patch(
        "awf.service.gc_request.run_service_workspace_gc",
        new=AsyncMock(side_effect=_fake_entrypoint),
    ):
        response = await client.post(
            "/v1/service/gc",
            json={
                "execute": True,
                "min_age_hours": 12,
                "limit": 5,
                "statuses": ["completed"],
                "exclude_statuses": ["failed"],
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    # The request params are forwarded to the API-side reclaim entrypoint.
    assert captured["execute"] is True
    assert captured["min_age_hours"] == 12
    assert captured["limit"] == 5
    assert list(captured["include_statuses"]) == [WorkspaceStatus.completed]
    assert list(captured["exclude_statuses"]) == [WorkspaceStatus.failed]
    # GC-B wiring (#389): the route threads the host home and the (default-on)
    # base-GC flag into the entrypoint.
    assert isinstance(captured["host_home"], Path)
    assert captured["reap_claude_bases"] is True
    # #582: ``execute`` delegates the capability-gated reclaim to the worker. With
    # no fresh worker heartbeat in this test env the delegation fast-fails to a
    # structured worker-unavailable outcome (never a false ``succeeded``), while
    # the API-side reclaim (deleted_path_count 3) is still reported.
    assert payload["status"] == "partial"
    assert payload["deleted_path_count"] == 3
    assert payload["worker_reclaim"]["reason_code"] == "SERVICE_GC_WORKER_UNAVAILABLE"


@pytest.mark.unit
async def test_service_gc_passes_disabled_base_reap_flag(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWF_WORK_DIR", str(tmp_path / "service"))
    monkeypatch.setenv("AWF_CLAUDE_BASE_GC_ENABLED", "false")
    captured: dict[str, object] = {}

    async def _fake_entrypoint(_session_factory: object, **kwargs: object) -> _FakeGCResult:
        captured.update(kwargs)
        return _FakeGCResult(
            {
                "dry_run": True,
                "status": "dry_run",
                "reason_code": "CLEANUP_DRY_RUN",
                "candidate_count": 0,
                "preserved_count": 0,
                "deleted_path_count": 0,
                "total_estimated_bytes": 0,
            }
        )

    with patch(
        "awf.service.gc_request.run_service_workspace_gc",
        new=AsyncMock(side_effect=_fake_entrypoint),
    ):
        response = await client.post("/v1/service/gc", json={})

    assert response.status_code == 200, response.text
    # The operator disabled GC-B; the route forwards the flag as-is.
    assert captured["reap_claude_bases"] is False


@pytest.mark.unit
async def test_service_gc_accepts_superseded_status_filter(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``superseded`` is a terminal GC status even though it is not a
    ``WorkspaceStatus`` enum value; the request schema must accept it for both
    include and exclude filters so API clients can target/exclude that class."""
    monkeypatch.setenv("AWF_WORK_DIR", str(tmp_path / "service"))
    captured: dict[str, object] = {}

    async def _fake_entrypoint(_session_factory: object, **kwargs: object) -> _FakeGCResult:
        captured.update(kwargs)
        return _FakeGCResult(
            {
                "dry_run": True,
                "status": "dry_run",
                "reason_code": "CLEANUP_DRY_RUN",
                "candidate_count": 0,
                "preserved_count": 0,
                "deleted_path_count": 0,
                "total_estimated_bytes": 0,
            }
        )

    with patch(
        "awf.service.gc_request.run_service_workspace_gc",
        new=AsyncMock(side_effect=_fake_entrypoint),
    ):
        response = await client.post(
            "/v1/service/gc",
            json={
                "statuses": ["superseded"],
                "exclude_statuses": ["superseded"],
            },
        )

    assert response.status_code == 200, response.text
    assert list(captured["include_statuses"]) == ["superseded"]
    assert list(captured["exclude_statuses"]) == ["superseded"]


@pytest.mark.unit
async def test_service_gc_returns_partial_envelope(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWF_WORK_DIR", str(tmp_path / "service"))

    fake = _FakeGCResult(
        {
            "dry_run": False,
            "status": "partial",
            "reason_code": "CLEANUP_EXECUTION_PARTIAL",
            "candidate_count": 1,
            "preserved_count": 0,
            "deleted_path_count": 0,
            "total_estimated_bytes": 0,
            "delete_errors": [{"reason_code": "PATH_DELETE_PERMISSION_DENIED", "kind": "auth"}],
        }
    )

    with patch(
        "awf.service.gc_request.run_service_workspace_gc",
        new=AsyncMock(return_value=fake),
    ):
        response = await client.post("/v1/service/gc", json={"execute": True})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "partial"
    # The API-side per-path delete errors still pass through unchanged.
    assert payload["delete_errors"][0]["reason_code"] == "PATH_DELETE_PERMISSION_DENIED"
    # #582: with no fresh worker heartbeat the execute delegation downgrades the
    # headline reason to the worker-unavailable code (the more actionable signal),
    # while the per-path errors remain inspectable in ``delete_errors``.
    assert payload["reason_code"] == "SERVICE_GC_WORKER_UNAVAILABLE"


@pytest.mark.unit
def test_plan_candidate_auth_dirs_skips_malformed_candidates() -> None:
    """Only well-formed candidate auth paths survive the fold.

    The candidates come from external worker JSON, so each shape guard must
    tolerate a malformed entry and skip it. A list mixing a non-dict entry, a
    candidate whose ``paths`` is not a dict, one whose ``paths['auth']`` is not a
    dict, and one whose ``auth['path']`` is not a string must all be dropped,
    leaving exactly the single fully well-formed auth ``Path``.
    """
    from awf.service.gc_request import _plan_candidate_auth_dirs

    plan_payload = {
        "candidates": [
            "not-a-dict",  # line 78: candidate is not a dict
            {"paths": "not-a-dict"},  # line 81: paths is not a dict
            {"paths": {"auth": "not-a-dict"}},  # line 84: auth is not a dict
            {"paths": {"auth": {"path": 1234}}},  # line 86: auth path is not a str
            {"paths": {"auth": {"path": "/var/lib/awf/auth/good"}}},  # well-formed
        ]
    }

    result = _plan_candidate_auth_dirs(plan_payload)

    assert result == frozenset({Path("/var/lib/awf/auth/good")})
