"""PR monitor adoption external_id / task_class persistence and policy tests."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import PullRequestMonitorAdoptionRequest
from awf.db.enums import TaskClass, WorkspaceStatus
from awf.db.models import Task, Workspace
from awf.db.repositories import TaskRepository, WorkspaceRepository
from awf.service import pr_monitor_adoption as adoption_module
from awf.service.pr_monitor_adoption import (
    PRMonitorAdoptionError,
    PullRequestMonitorAdoptionService,
)
from tests.unit.service.test_pr_monitor_adoption_parts.test_pr_monitor_adoption_part_001 import (
    _count,
    _metadata,
    _MetadataFetcher,
    factory,
)

_IMPORTED_FIXTURES = (factory,)


class TestPullRequestMonitorAdoptionExternalIdTaskClass:
    @pytest.mark.unit
    async def test_persists_explicit_external_id_and_task_class(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with factory() as session:
            result = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    external_id="CLOUD-TASK-42",
                    task_class=TaskClass.test_task,
                )
            )
            await session.commit()

        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(result.workspace_id)
            task = await session.get(Task, result.task_id)
            assert workspace is not None
            assert task is not None
            assert workspace.task_external_id == "CLOUD-TASK-42"
            assert workspace.task_class == "test_task"
            assert task.external_id == "CLOUD-TASK-42"
            assert task.task_class == "test_task"

    @pytest.mark.unit
    async def test_omitted_fields_keep_generated_external_id_and_null_class(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        expected = adoption_module._adoption_external_id(
            repo_slug="dimileeh/aira-web",
            pr_number=277,
        )
        async with factory() as session:
            result = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                )
            )
            await session.commit()

        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(result.workspace_id)
            task = await session.get(Task, result.task_id)
            assert workspace is not None
            assert task is not None
            assert workspace.task_external_id == expected
            assert workspace.task_class is None
            assert task.external_id == expected
            assert task.task_class is None

    @pytest.mark.unit
    async def test_same_policy_replay_attaches_existing(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        request = PullRequestMonitorAdoptionRequest(
            repo_slug="dimileeh/aira-web",
            pr_number=277,
            external_id="CLOUD-TASK-42",
            task_class=TaskClass.docs_task,
        )
        fetcher = _MetadataFetcher(_metadata())
        async with factory() as session:
            first = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=fetcher,
            ).adopt(request)
            second = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=fetcher,
            ).adopt(request)
            await session.commit()

        assert second.attached_existing is True
        assert second.workspace_id == first.workspace_id
        assert fetcher.calls == [("dimileeh/aira-web", 277)]

    @pytest.mark.unit
    async def test_different_external_id_or_task_class_conflicts_without_secret_echo(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        secret_like = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
        async with factory() as session:
            await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    external_id=secret_like,
                    task_class=TaskClass.test_task,
                )
            )
            await session.commit()

        async with factory() as session:
            service = PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            )
            with pytest.raises(PRMonitorAdoptionError) as id_exc:
                await service.adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_slug="dimileeh/aira-web",
                        pr_number=277,
                        external_id="OTHER-ID",
                        task_class=TaskClass.test_task,
                    )
                )
            with pytest.raises(PRMonitorAdoptionError) as class_exc:
                await service.adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_slug="dimileeh/aira-web",
                        pr_number=277,
                        external_id=secret_like,
                        task_class=TaskClass.docs_task,
                    )
                )

        assert id_exc.value.error_code == "PR_ADOPTION_POLICY_CONFLICT"
        assert class_exc.value.error_code == "PR_ADOPTION_POLICY_CONFLICT"
        assert secret_like not in str(id_exc.value.detail)
        assert secret_like not in str(class_exc.value.detail)
        assert secret_like not in id_exc.value.message
        assert secret_like not in class_exc.value.message

    @pytest.mark.unit
    async def test_omitted_replay_conflicts_with_explicit_identity_workspace(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with factory() as session:
            await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    external_id="CLOUD-TASK-42",
                )
            )
            await session.commit()

        async with factory() as session:
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await PullRequestMonitorAdoptionService(
                    session,
                    metadata_fetcher=_MetadataFetcher(_metadata()),
                ).adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_slug="dimileeh/aira-web",
                        pr_number=277,
                    )
                )

        assert excinfo.value.error_code == "PR_ADOPTION_POLICY_CONFLICT"

    @pytest.mark.unit
    async def test_terminal_readoption_reuses_explicit_external_id_via_supersession(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with factory() as session:
            first = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(title="feature: first")),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    external_id="CLOUD-TASK-42",
                    task_class=TaskClass.test_task,
                )
            )
            old_workspace = await WorkspaceRepository(session).get(first.workspace_id)
            assert old_workspace is not None
            old_workspace.status = WorkspaceStatus.destroyed.value
            await session.commit()

        async with factory() as session:
            second = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(title="feature: second")),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    external_id="CLOUD-TASK-42",
                    task_class=TaskClass.test_task,
                )
            )
            await session.commit()

        assert second.attached_existing is False
        assert second.workspace_id != first.workspace_id
        async with factory() as session:
            old_workspace = await WorkspaceRepository(session).get(first.workspace_id)
            old_task = await session.get(Task, first.task_id)
            fresh_workspace = await WorkspaceRepository(session).get(second.workspace_id)
            fresh_task = await session.get(Task, second.task_id)
            assert old_workspace is not None
            assert old_task is not None
            assert fresh_workspace is not None
            assert fresh_task is not None
            assert old_workspace.task_external_id == (
                adoption_module._superseded_adoption_external_id(
                    external_id="CLOUD-TASK-42",
                    workspace_id=first.workspace_id,
                )
            )
            assert old_task.external_id == old_workspace.task_external_id
            assert fresh_workspace.task_external_id == "CLOUD-TASK-42"
            assert fresh_task.external_id == "CLOUD-TASK-42"
            assert fresh_workspace.task_class == "test_task"
            assert fresh_task.task_class == "test_task"
            assert await _count(session, Task) == 2

    @pytest.mark.unit
    async def test_terminal_readoption_releases_explicit_id_containing_superseded_marker(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Caller IDs may embed ``:superseded:``; supersession must still free the slot.

        Substring detection would leave the terminal task holding the requested ID,
        so a later re-adoption with a changed PR title raises TASK_EXTERNAL_ID_CONFLICT.
        """
        explicit_id = "LEGACY:superseded:TICKET-9"
        async with factory() as session:
            first = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(title="feature: first")),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    external_id=explicit_id,
                    task_class=TaskClass.test_task,
                )
            )
            old_workspace = await WorkspaceRepository(session).get(first.workspace_id)
            assert old_workspace is not None
            old_workspace.status = WorkspaceStatus.destroyed.value
            await session.commit()

        async with factory() as session:
            second = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(title="feature: second")),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    external_id=explicit_id,
                    task_class=TaskClass.test_task,
                )
            )
            await session.commit()

        assert second.attached_existing is False
        assert second.workspace_id != first.workspace_id
        async with factory() as session:
            old_workspace = await WorkspaceRepository(session).get(first.workspace_id)
            old_task = await session.get(Task, first.task_id)
            fresh_workspace = await WorkspaceRepository(session).get(second.workspace_id)
            fresh_task = await session.get(Task, second.task_id)
            assert old_workspace is not None
            assert old_task is not None
            assert fresh_workspace is not None
            assert fresh_task is not None
            expected_superseded = adoption_module._superseded_adoption_external_id(
                external_id=explicit_id,
                workspace_id=first.workspace_id,
            )
            assert old_workspace.task_external_id == expected_superseded
            assert old_task.external_id == expected_superseded
            assert fresh_workspace.task_external_id == explicit_id
            assert fresh_task.external_id == explicit_id
            assert await _count(session, Task) == 2

    @pytest.mark.unit
    async def test_terminal_readoption_with_changed_identity_releases_prior_external_id(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Terminal supersession must free the prior explicit ID even when the next
        generation omits external_id (effective ID changes), so a later re-adoption
        with the original explicit ID can create a fresh monitor.
        """
        async with factory() as session:
            first = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(title="feature: first")),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    external_id="CLOUD-TASK-42",
                )
            )
            old_workspace = await WorkspaceRepository(session).get(first.workspace_id)
            assert old_workspace is not None
            old_workspace.status = WorkspaceStatus.destroyed.value
            await session.commit()

        async with factory() as session:
            second = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(title="feature: second")),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                )
            )
            await session.commit()

        assert second.attached_existing is False
        assert second.workspace_id != first.workspace_id
        async with factory() as session:
            old_workspace = await WorkspaceRepository(session).get(first.workspace_id)
            old_task = await session.get(Task, first.task_id)
            assert old_workspace is not None
            assert old_task is not None
            assert old_workspace.task_external_id == (
                adoption_module._superseded_adoption_external_id(
                    external_id="CLOUD-TASK-42",
                    workspace_id=first.workspace_id,
                )
            )
            assert old_task.external_id == old_workspace.task_external_id
            second_workspace = await WorkspaceRepository(session).get(second.workspace_id)
            assert second_workspace is not None
            second_workspace.status = WorkspaceStatus.destroyed.value
            await session.commit()

        async with factory() as session:
            third = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(title="feature: third")),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    external_id="CLOUD-TASK-42",
                )
            )
            await session.commit()

        assert third.attached_existing is False
        assert third.workspace_id != second.workspace_id
        async with factory() as session:
            fresh_workspace = await WorkspaceRepository(session).get(third.workspace_id)
            fresh_task = await session.get(Task, third.task_id)
            assert fresh_workspace is not None
            assert fresh_task is not None
            assert fresh_workspace.task_external_id == "CLOUD-TASK-42"
            assert fresh_task.external_id == "CLOUD-TASK-42"

    @pytest.mark.unit
    async def test_explicit_id_collision_with_unrelated_task_does_not_corrupt(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        secret_like = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
        async with factory() as session:
            unrelated = await TaskRepository(session).create_or_get(
                repo_url="https://github.com/other/repo.git",
                base_branch="main",
                title="unrelated",
                prompt="unrelated prompt",
                external_id=secret_like,
                idempotency_key="unrelated-key",
                task_class="docs_task",
                owned_paths=[],
            )
            await session.commit()
            unrelated_id = unrelated.id
            unrelated_external_id = unrelated.external_id
            unrelated_title = unrelated.title

        async with factory() as session:
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await PullRequestMonitorAdoptionService(
                    session,
                    metadata_fetcher=_MetadataFetcher(_metadata()),
                ).adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_slug="dimileeh/aira-web",
                        pr_number=277,
                        external_id=secret_like,
                    )
                )
            # Commit after the domain error (mirrors get_db_session / careless
            # callers). Savepoint atomicity must leave no orphan adoption rows.
            await session.commit()

        assert excinfo.value.error_code == "TASK_EXTERNAL_ID_CONFLICT"
        assert secret_like not in str(excinfo.value.detail)
        assert secret_like not in excinfo.value.message
        async with factory() as session:
            task = await session.get(Task, unrelated_id)
            assert task is not None
            assert task.external_id == unrelated_external_id
            assert task.title == unrelated_title
            assert await _count(session, Workspace) == 0
            assert await _count(session, Task) == 1

    @pytest.mark.unit
    async def test_terminal_readoption_collision_does_not_supersede_or_orphan(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Colliding explicit ID during terminal re-adopt must not supersede."""
        colliding_id = "shared-external-collision"
        async with factory() as session:
            first = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(title="feature: first")),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    external_id="prior-adoption-id",
                )
            )
            terminal = await WorkspaceRepository(session).get(first.workspace_id)
            assert terminal is not None
            terminal_idempotency_key = terminal.idempotency_key
            terminal_external_id = terminal.task_external_id
            terminal.status = WorkspaceStatus.destroyed.value
            await session.commit()

        async with factory() as session:
            await TaskRepository(session).create_or_get(
                repo_url="https://github.com/other/repo.git",
                base_branch="main",
                title="unrelated owner",
                prompt="unrelated prompt",
                external_id=colliding_id,
                idempotency_key="unrelated-collision-key",
                task_class="docs_task",
                owned_paths=[],
            )
            await session.commit()

        async with factory() as session:
            with pytest.raises(PRMonitorAdoptionError) as excinfo:
                await PullRequestMonitorAdoptionService(
                    session,
                    metadata_fetcher=_MetadataFetcher(_metadata(title="feature: retry")),
                ).adopt(
                    PullRequestMonitorAdoptionRequest(
                        repo_slug="dimileeh/aira-web",
                        pr_number=277,
                        external_id=colliding_id,
                    )
                )
            await session.commit()

        assert excinfo.value.error_code == "TASK_EXTERNAL_ID_CONFLICT"
        async with factory() as session:
            terminal = await WorkspaceRepository(session).get(first.workspace_id)
            terminal_task = await session.get(Task, first.task_id)
            assert terminal is not None
            assert terminal_task is not None
            assert terminal.idempotency_key == terminal_idempotency_key
            assert terminal.task_external_id == terminal_external_id
            assert terminal_task.external_id == terminal_external_id
            assert await _count(session, Workspace) == 1
            assert await _count(session, Task) == 2

    @pytest.mark.unit
    async def test_terminal_readoption_dodges_occupied_superseded_external_id(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Preferred superseded slot may already be owned; allocate a free alternate.

        A caller can create an external ID equal to ``{old}:superseded:{workspace_id}``
        after observing a terminal adoption. Rewriting into that occupied slot must
        not raise IntegrityError and wedge every later re-adoption.
        """
        explicit_id = "CLOUD-TASK-42"
        async with factory() as session:
            first = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(title="feature: first")),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    external_id=explicit_id,
                )
            )
            old_workspace = await WorkspaceRepository(session).get(first.workspace_id)
            assert old_workspace is not None
            preferred_superseded = adoption_module._superseded_adoption_external_id(
                external_id=explicit_id,
                workspace_id=first.workspace_id,
            )
            await TaskRepository(session).create_or_get(
                repo_url="https://github.com/other/repo.git",
                base_branch="main",
                title="occupies preferred superseded slot",
                prompt="unrelated prompt",
                external_id=preferred_superseded,
                idempotency_key="unrelated-superseded-slot",
                task_class="docs_task",
                owned_paths=[],
            )
            old_workspace.status = WorkspaceStatus.destroyed.value
            await session.commit()

        async with factory() as session:
            second = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(title="feature: second")),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    external_id=explicit_id,
                )
            )
            await session.commit()

        assert second.attached_existing is False
        assert second.workspace_id != first.workspace_id
        alternate = adoption_module._superseded_adoption_external_id(
            external_id=explicit_id,
            workspace_id=first.workspace_id,
            collision_nonce=1,
        )
        async with factory() as session:
            old_workspace = await WorkspaceRepository(session).get(first.workspace_id)
            old_task = await session.get(Task, first.task_id)
            fresh_workspace = await WorkspaceRepository(session).get(second.workspace_id)
            fresh_task = await session.get(Task, second.task_id)
            occupant = await TaskRepository(session).get_by_ref(preferred_superseded)
            assert old_workspace is not None
            assert old_task is not None
            assert fresh_workspace is not None
            assert fresh_task is not None
            assert occupant is not None
            assert occupant.external_id == preferred_superseded
            assert old_workspace.task_external_id == alternate
            assert old_task.external_id == alternate
            assert alternate != preferred_superseded
            assert adoption_module._is_superseded_adoption_external_id(
                alternate,
                workspace_id=first.workspace_id,
            )
            assert fresh_workspace.task_external_id == explicit_id
            assert fresh_task.external_id == explicit_id
            assert await _count(session, Task) == 3

    @pytest.mark.unit
    def test_superseded_external_id_stays_within_column_bounds(self) -> None:
        workspace_id = "ws_" + ("a" * 24)
        short_id = "CLOUD-TASK-42"
        assert (
            adoption_module._superseded_adoption_external_id(
                external_id=short_id,
                workspace_id=workspace_id,
            )
            == f"{short_id}:superseded:{workspace_id}"
        )
        salted = adoption_module._superseded_adoption_external_id(
            external_id=short_id,
            workspace_id=workspace_id,
            collision_nonce=1,
        )
        assert salted != f"{short_id}:superseded:{workspace_id}"
        assert len(salted) <= 128
        assert adoption_module._is_superseded_adoption_external_id(
            salted,
            workspace_id=workspace_id,
        )

        # Accepted explicit IDs may be 128 chars; naive append exceeds String(128).
        long_id = "E" * 90
        superseded = adoption_module._superseded_adoption_external_id(
            external_id=long_id,
            workspace_id=workspace_id,
        )
        assert len(superseded) <= 128
        assert adoption_module._is_superseded_adoption_external_id(
            superseded,
            workspace_id=workspace_id,
        )
        assert superseded != long_id
        other = adoption_module._superseded_adoption_external_id(
            external_id="F" * 90,
            workspace_id=workspace_id,
        )
        assert other != superseded
        assert len(other) <= 128

        # Pathological workspace ids still produce a bounded unique slot.
        huge_workspace_id = "ws_" + ("b" * 200)
        pathological = adoption_module._superseded_adoption_external_id(
            external_id=long_id,
            workspace_id=huge_workspace_id,
        )
        assert len(pathological) <= 128
        assert adoption_module._is_superseded_adoption_external_id(
            pathological,
            workspace_id=huge_workspace_id,
        )
        # Caller-controlled IDs that merely embed the marker are not "already released".
        assert not adoption_module._is_superseded_adoption_external_id(
            "LEGACY:superseded:TICKET-9",
            workspace_id=workspace_id,
        )

    @pytest.mark.unit
    async def test_terminal_readoption_bounds_long_explicit_external_id(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        long_external_id = "E" * 90
        async with factory() as session:
            first = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(title="feature: first")),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    external_id=long_external_id,
                )
            )
            old_workspace = await WorkspaceRepository(session).get(first.workspace_id)
            assert old_workspace is not None
            old_workspace.status = WorkspaceStatus.destroyed.value
            await session.commit()

        async with factory() as session:
            second = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata(title="feature: second")),
            ).adopt(
                PullRequestMonitorAdoptionRequest(
                    repo_slug="dimileeh/aira-web",
                    pr_number=277,
                    external_id=long_external_id,
                )
            )
            await session.commit()

        assert second.attached_existing is False
        assert second.workspace_id != first.workspace_id
        async with factory() as session:
            old_workspace = await WorkspaceRepository(session).get(first.workspace_id)
            old_task = await session.get(Task, first.task_id)
            fresh_workspace = await WorkspaceRepository(session).get(second.workspace_id)
            fresh_task = await session.get(Task, second.task_id)
            assert old_workspace is not None
            assert old_task is not None
            assert fresh_workspace is not None
            assert fresh_task is not None
            expected_superseded = adoption_module._superseded_adoption_external_id(
                external_id=long_external_id,
                workspace_id=first.workspace_id,
            )
            assert len(expected_superseded) <= 128
            assert old_workspace.task_external_id == expected_superseded
            assert old_task.external_id == expected_superseded
            assert fresh_workspace.task_external_id == long_external_id
            assert fresh_task.external_id == long_external_id
            assert await _count(session, Task) == 2
