"""Workspace owned-path conflict policy tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import awf.db.repositories as repositories
from awf.api.schemas import WorkspaceCreateRequest
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import QueueDecisionRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.workspaces import WorkspaceService
from tests.postgres import postgres_test_engine


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _request(
    *,
    repo_url: str = "git@github.com:example/service.git",
    base_branch: str = "development",
    title: str = "Owned path policy",
    task_class: str | None = None,
    owned_paths: list[str] | None = None,
    workspace_profile: dict[str, object] | None = None,
) -> WorkspaceCreateRequest:
    task = {
        "title": title,
        "prompt": "Do policy-sensitive work.",
        "agent": "codex",
        "kind": "feature_branch_pr",
        "owned_paths": list(owned_paths or []),
    }
    if task_class is not None:
        task["task_class"] = task_class
    return WorkspaceCreateRequest(
        repo={"url": repo_url, "base_branch": base_branch},
        task=task,
        workspace={
            "profile_ref": None if workspace_profile is not None else "auto",
            "profile": workspace_profile,
        },
        validation={"commands": ["pytest -q"], "requested_tier": 1},
        resources={},
        preflight={
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "owned path policy test fixture",
        },
    )


@pytest.mark.unit
async def test_create_allows_empty_requested_owned_paths(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    await service.create(_request(title="existing", owned_paths=["src/awf/api/**"]))

    created = await service.create(_request(title="new", owned_paths=[]))

    assert created.owned_paths == []


@pytest.mark.unit
async def test_create_allows_non_overlapping_owned_paths(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    await service.create(_request(title="existing", owned_paths=["src/awf/api/**"]))

    created = await service.create(_request(title="new", owned_paths=["docs/**"]))

    assert created.owned_paths == ["docs/**"]


@pytest.mark.unit
async def test_create_ignores_terminal_and_teardown_conflicts(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    existing = await service.create(_request(title="existing", owned_paths=["src/awf/api/**"]))
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(existing.id)
        assert workspace is not None
        workspace.status = WorkspaceStatus.completed.value
        await session.commit()

    created = await service.create(
        _request(title="new", owned_paths=["src/awf/api/routes/workspaces.py"])
    )

    assert created.owned_paths == ["src/awf/api/routes/workspaces.py"]


@pytest.mark.unit
async def test_create_allows_overlap_and_records_risk_event(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    existing = await service.create(
        _request(
            title="existing",
            task_class="refactor_task",
            owned_paths=["src/awf/service/**"],
        )
    )

    created = await service.create(
        _request(
            title="new",
            task_class="docs_task",
            owned_paths=["src/awf/service/workspaces.py"],
        )
    )
    events = await service.list_events(
        created.id,
        event_type="workspace.owned_path_overlap_risk",
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(created.id)
        decisions = await QueueDecisionRepository(session).list_for_workspace(created.id)

    assert workspace is not None
    assert workspace.status == WorkspaceStatus.requested.value
    assert created.owned_paths == ["src/awf/service/workspaces.py"]
    assert len(decisions) == 1
    assert decisions[0].decision == "admitted"
    assert decisions[0].overlap_risk_summary == {
        "warning_code": "OWNED_PATH_OVERLAP_RISK",
        "overlap_count": 1,
        "workspace_ids": [existing.id],
        "overlaps": [
            {
                "workspace_id": existing.id,
                "existing_path": "src/awf/service/**",
                "requested_path": "src/awf/service/workspaces.py",
            }
        ],
    }
    assert events is not None
    assert len(events) == 1
    assert events[0].reason_code == "OWNED_PATH_OVERLAP_RISK"
    assert events[0].payload == {
        "warning_code": "OWNED_PATH_OVERLAP_RISK",
        "message": (
            "Owned paths overlap active workspaces; this may require rebase or conflict resolution."
        ),
        "workspace_ids": [existing.id],
        "overlaps": [
            {
                "workspace_id": existing.id,
                "existing_path": "src/awf/service/**",
                "requested_path": "src/awf/service/workspaces.py",
            }
        ],
    }


@pytest.mark.unit
async def test_create_attaches_overlap_coordination_context_to_workspace_metadata(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    existing = await service.create(
        _request(
            title="existing service work",
            task_class="refactor_task",
            owned_paths=["src/awf/service/**"],
        )
    )

    created = await service.create(
        _request(
            title="new service file work",
            task_class="docs_task",
            owned_paths=["src/awf/service/workspaces.py"],
        )
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(created.id)
        decisions = await QueueDecisionRepository(session).list_for_workspace(created.id)

    assert workspace is not None
    assert workspace.status == WorkspaceStatus.requested.value
    assert len(decisions) == 1
    assert decisions[0].decision == "admitted"
    assert decisions[0].reason_code == "ADMITTED_LOCAL"

    warnings = workspace.task_policy["coordination"]["warnings"]
    assert len(warnings) == 1
    warning = warnings[0]
    assert warning["warning_code"] == "OWNED_PATH_OVERLAP_RISK"
    assert warning["severity"] == "advisory"
    assert warning["blocks_launch"] is False
    assert warning["workspace_ids"] == [existing.id]
    assert warning["overlaps"] == [
        {
            "workspace_id": existing.id,
            "existing_path": "src/awf/service/**",
            "requested_path": "src/awf/service/workspaces.py",
            "match_reason_code": "OWNED_PATH_WILDCARD_MATCH",
            "explanation": (
                "Wildcard owned-path prefixes overlap: "
                "src/awf/service/** <-> src/awf/service/workspaces.py."
            ),
        }
    ]
    assert warning["stale_policy_context"] == {
        "trigger_type": "path_overlap",
        "stale_reason_code": "STALE_OVERLAP",
    }


@pytest.mark.unit
async def test_direct_create_custom_plan_path_keeps_real_ws_file_overlap_warning(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = iter(
        [
            "ws_aaaaaaaaaaaaaaaaaaaaaaaa",
            "ws_bbbbbbbbbbbbbbbbbbbbbbbb",
        ]
    )
    monkeypatch.setattr(repositories, "new_workspace_id", lambda: next(ids))
    service = WorkspaceService(factory)
    real_docs_path = "docs/ws_0123456789abcdef01234567.md"
    custom_profile: dict[str, object] = {
        "name": "custom-plan-paths",
        "planning": {"required": True, "plan_path": "docs/{workspace_id}.md"},
    }
    existing = await service.create(
        _request(
            title="existing real ws-shaped docs file",
            owned_paths=[real_docs_path],
        )
    )

    created = await service.create(
        _request(
            title="new real ws-shaped docs file",
            owned_paths=[real_docs_path],
            workspace_profile=custom_profile,
        )
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(created.id)
        decisions = await QueueDecisionRepository(session).list_for_workspace(created.id)

    assert existing.id == "ws_aaaaaaaaaaaaaaaaaaaaaaaa"
    assert created.id == "ws_bbbbbbbbbbbbbbbbbbbbbbbb"
    assert workspace is not None
    warning = workspace.task_policy["coordination"]["warnings"][0]
    assert warning["workspace_ids"] == [existing.id]
    assert warning["overlaps"][0] == {
        "workspace_id": existing.id,
        "existing_path": real_docs_path,
        "requested_path": real_docs_path,
        "match_reason_code": "OWNED_PATH_EXACT_MATCH",
        "explanation": f"Owned paths normalize to the same path: {real_docs_path}.",
    }
    assert decisions[0].overlap_risk_summary["workspace_ids"] == [existing.id]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("task_class", "existing_path", "requested_path"),
    [
        ("refactor_task", "src/awf/api/**", "src/awf/api/routes/workspaces.py"),
        ("docs_task", "docs/**", "docs/owned-path-policy.md"),
        ("test_task", "tests/unit/**", "tests/unit/service/test_workspaces.py"),
        ("migration_task", "migrations/**", "migrations/202604260001_add_index.sql"),
        ("dependency_task", "pyproject.toml", "pyproject.toml"),
        ("build_config_task", "Dockerfile", "Dockerfile"),
    ],
)
async def test_create_overlap_is_advisory_for_all_current_task_classes(
    factory: async_sessionmaker[AsyncSession],
    task_class: str,
    existing_path: str,
    requested_path: str,
) -> None:
    service = WorkspaceService(factory)
    existing = await service.create(
        _request(
            title=f"existing {task_class}",
            task_class=task_class,
            owned_paths=[existing_path],
        )
    )

    created = await service.create(
        _request(
            title=f"new {task_class}",
            task_class=task_class,
            owned_paths=[requested_path],
        )
    )
    events = await service.list_events(
        created.id,
        event_type="workspace.owned_path_overlap_risk",
    )

    assert created.owned_paths == [requested_path]
    assert events is not None
    assert events[0].payload is not None
    assert events[0].payload["workspace_ids"] == [existing.id]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("task_class", "existing_path", "requested_path"),
    [
        ("migration_task", "migrations/**", "migrations/202604260001_add_index.sql"),
        ("dependency_task", "pyproject.toml", "pyproject.toml"),
        ("build_config_task", "Dockerfile", "Dockerfile"),
    ],
)
async def test_overlap_coordination_context_is_non_blocking_for_serial_high_risk_classes(
    factory: async_sessionmaker[AsyncSession],
    task_class: str,
    existing_path: str,
    requested_path: str,
) -> None:
    service = WorkspaceService(factory)
    existing = await service.create(
        _request(
            title=f"existing {task_class}",
            task_class=task_class,
            owned_paths=[existing_path],
        )
    )

    created = await service.create(
        _request(
            title=f"new {task_class}",
            task_class=task_class,
            owned_paths=[requested_path],
        )
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(created.id)
        decisions = await QueueDecisionRepository(session).list_for_workspace(created.id)

    assert workspace is not None
    assert workspace.status == WorkspaceStatus.requested.value
    assert decisions[0].decision == "admitted"
    assert decisions[0].reason_code == "ADMITTED_LOCAL"
    assert decisions[0].overlap_risk_summary["warning_code"] == "OWNED_PATH_OVERLAP_RISK"
    assert decisions[0].overlap_risk_summary["workspace_ids"] == [existing.id]
    warning = workspace.task_policy["coordination"]["warnings"][0]
    assert warning["severity"] == "advisory"
    assert warning["blocks_launch"] is False
    assert warning["workspace_ids"] == [existing.id]
