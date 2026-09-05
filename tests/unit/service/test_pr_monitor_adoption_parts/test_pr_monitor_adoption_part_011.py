"""Adoption seeding from a terminal predecessor + operator hint (issue #911)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import PullRequestMonitorAdoptionRequest
from awf.common.github_client import PullRequestAdoptionMetadata, RepoRef
from awf.db.enums import WorkspaceStatus
from awf.db.models import Operation, Workspace, WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.operator_hints import OPERATOR_HINT_STATE_KEY, operator_hint_from_threads
from awf.runtime.pr_monitor import (
    AddressOperatorHint,
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    PRStatus,
    decide,
)
from awf.runtime.pr_monitor_models import ReviewComment
from awf.service.pr_monitor_adoption import PullRequestMonitorAdoptionService
from awf.service.pr_monitor_adoption_helpers import PR_ADOPTION_REQUESTED_EVENT_TYPE
from awf.service.pr_monitor_adoption_seed import (
    PR_ADOPTION_OPERATOR_HINT_REASON,
    PR_ADOPTION_SEEDED_EVENT_TYPE,
    PR_ADOPTION_SEEDED_REASON,
)
from tests.postgres import postgres_test_engine

REPO_SLUG = "dimileeh/aira-infra"
PR_NUMBER = 229

# The aira-infra PR #229 shape: verdicts the predecessor already dispositioned,
# their evidence markers, plus run-local bookkeeping that must stay behind.
_SEEDABLE_PREDECESSOR_STATE: dict[str, str] = {
    "5120013294": "false_positive",
    "issue:5549804922": "false_positive",
    "issue:5549805025": "false_positive",
    "PRRT_kwDOSJAM6s6fNhZo": "fix_committed",
    "__review_comment_body_hash__:5120013294": "a" * 64,
    "__deferred_issue_filed__:PRRT_kwDOSJAM6s6fNhZo:abc123": f"{REPO_SLUG}#42",
}
_NEVER_COPIED_PREDECESSOR_STATE: dict[str, str] = {
    "__awf_protected_block_preserved_head__": "d" * 40,
    "__awf_protected_block__:PRRT_kwDOSJAM6s6fNhZo": "blocked",
    f"__awf_awaiting_required_checks_first_seen__:{PR_NUMBER}:" + "d" * 40: "1700000000",
    OPERATOR_HINT_STATE_KEY: json.dumps({"reason": "prior directive", "status": "pending"}),
    "__awf_operator_hint_processed__:op_prior": "processed",
    f"__awf_awaiting_workflow_scope__:{PR_NUMBER}": "armed",
    f"__awf_merge_block_attention__:{PR_NUMBER}": "notified",
}


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _metadata(*, head_sha: str = "h" * 40) -> PullRequestAdoptionMetadata:
    return PullRequestAdoptionMetadata(
        number=PR_NUMBER,
        head_ref="feature/ready",
        head_repo_slug=REPO_SLUG,
        base_ref="main",
        head_sha=head_sha,
        base_sha="b" * 40,
        state="OPEN",
        is_draft=False,
        closed=False,
        merged=False,
        author="octocat",
        url=f"https://github.com/{REPO_SLUG}/pull/{PR_NUMBER}",
        title="fix: hardening",
    )


class _MetadataFetcher:
    def __init__(self, metadata: PullRequestAdoptionMetadata) -> None:
        self.metadata = metadata

    async def __call__(self, *, repo: RepoRef, pr_number: int) -> PullRequestAdoptionMetadata:
        return self.metadata


def _request(**overrides: Any) -> PullRequestMonitorAdoptionRequest:
    return PullRequestMonitorAdoptionRequest(
        repo_slug=REPO_SLUG,
        pr_number=PR_NUMBER,
        **overrides,
    )


async def _adopt(
    factory: async_sessionmaker[AsyncSession],
    *,
    head_sha: str = "h" * 40,
    **overrides: Any,
) -> str:
    async with factory() as session:
        response = await PullRequestMonitorAdoptionService(
            session,
            metadata_fetcher=_MetadataFetcher(_metadata(head_sha=head_sha)),
        ).adopt(_request(**overrides))
        await session.commit()
    return response.workspace_id


async def _fail_with_monitor_state(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    monitor_state: dict[str, str],
) -> None:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = dict(monitor_state)
        await repo.transition(workspace, to=WorkspaceStatus.failed, reason_code="TEST_FAIL")
        await session.commit()


async def _monitor_state(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> dict[str, str]:
    async with factory() as session:
        workspace = await session.get(Workspace, workspace_id)
        assert workspace is not None
        return dict(workspace.monitor_threads_addressed or {})


async def _events(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    event_type: str,
) -> list[WorkspaceEvent]:
    async with factory() as session:
        return list(
            (
                await session.execute(
                    select(WorkspaceEvent)
                    .where(
                        WorkspaceEvent.workspace_id == workspace_id,
                        WorkspaceEvent.event_type == event_type,
                    )
                    .order_by(WorkspaceEvent.occurred_at.asc(), WorkspaceEvent.event_order.asc())
                )
            ).scalars()
        )


class TestPullRequestMonitorAdoptionSeedingPart011:
    @pytest.mark.unit
    async def test_seeds_only_allowlisted_keys_from_terminal_predecessor(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        previous_id = await _adopt(factory)
        await _fail_with_monitor_state(
            factory,
            previous_id,
            {**_SEEDABLE_PREDECESSOR_STATE, **_NEVER_COPIED_PREDECESSOR_STATE},
        )

        fresh_id = await _adopt(factory)

        assert fresh_id != previous_id
        assert await _monitor_state(factory, fresh_id) == _SEEDABLE_PREDECESSOR_STATE

    @pytest.mark.unit
    async def test_seeded_event_records_predecessor_and_copied_keys(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        previous_id = await _adopt(factory)
        await _fail_with_monitor_state(
            factory,
            previous_id,
            {**_SEEDABLE_PREDECESSOR_STATE, **_NEVER_COPIED_PREDECESSOR_STATE},
        )

        fresh_id = await _adopt(factory)

        events = await _events(factory, fresh_id, PR_ADOPTION_SEEDED_EVENT_TYPE)
        assert len(events) == 1
        event = events[0]
        assert event.reason_code == PR_ADOPTION_SEEDED_REASON
        payload = event.payload or {}
        assert payload["predecessor_workspace_id"] == previous_id
        assert payload["copied_keys"] == sorted(_SEEDABLE_PREDECESSOR_STATE)
        assert payload["copied_key_count"] == len(_SEEDABLE_PREDECESSOR_STATE)
        assert payload["repo_slug"] == REPO_SLUG
        assert payload["pr_number"] == PR_NUMBER

    @pytest.mark.unit
    async def test_moved_head_drops_inherited_fix_committed_only(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A force-push / revert before re-adoption invalidates ``fix_committed``.

        The fix the predecessor recorded need not exist in the head the successor
        adopts, while the comment itself is unchanged — inheriting the verdict
        would suppress live feedback and let auto-merge run over it. Dispositions
        of the *comment* (``false_positive`` and friends) are head-independent and
        still cross.
        """
        previous_id = await _adopt(factory)
        await _fail_with_monitor_state(factory, previous_id, _SEEDABLE_PREDECESSOR_STATE)

        fresh_id = await _adopt(factory, head_sha="f" * 40)

        expected = {
            key: value
            for key, value in _SEEDABLE_PREDECESSOR_STATE.items()
            if value != "fix_committed"
        }
        assert "PRRT_kwDOSJAM6s6fNhZo" not in expected
        assert await _monitor_state(factory, fresh_id) == expected
        events = await _events(factory, fresh_id, PR_ADOPTION_SEEDED_EVENT_TYPE)
        assert (events[0].payload or {})["copied_keys"] == sorted(expected)

    @pytest.mark.unit
    async def test_first_adoption_seeds_nothing_and_emits_no_seeded_event(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fresh_id = await _adopt(factory)

        assert await _monitor_state(factory, fresh_id) == {}
        assert await _events(factory, fresh_id, PR_ADOPTION_SEEDED_EVENT_TYPE) == []

    @pytest.mark.unit
    async def test_predecessor_without_copyable_state_emits_no_seeded_event(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        previous_id = await _adopt(factory)
        await _fail_with_monitor_state(factory, previous_id, _NEVER_COPIED_PREDECESSOR_STATE)

        fresh_id = await _adopt(factory)

        assert await _monitor_state(factory, fresh_id) == {}
        assert await _events(factory, fresh_id, PR_ADOPTION_SEEDED_EVENT_TYPE) == []

    @pytest.mark.unit
    async def test_attaching_to_live_adoption_neither_seeds_nor_emits_event(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        first_id = await _adopt(factory)

        async with factory() as session:
            attached = await PullRequestMonitorAdoptionService(
                session,
                metadata_fetcher=_MetadataFetcher(_metadata()),
            ).adopt(_request(hint="ignored on the attach path"))
            await session.commit()

        assert attached.attached_existing is True
        assert attached.workspace_id == first_id
        assert await _monitor_state(factory, first_id) == {}
        assert await _events(factory, first_id, PR_ADOPTION_SEEDED_EVENT_TYPE) == []

    @pytest.mark.unit
    async def test_hint_arms_pending_operator_hint_bound_to_the_adoption_operation(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        directive = "do NOT edit .github/workflows/*"

        workspace_id = await _adopt(factory, hint=directive)

        state = await _monitor_state(factory, workspace_id)
        assert OPERATOR_HINT_STATE_KEY in state
        payload = json.loads(state[OPERATOR_HINT_STATE_KEY])
        assert payload["status"] == "pending"
        assert payload["directive"] == directive
        assert payload["reason_code"] == PR_ADOPTION_OPERATOR_HINT_REASON
        async with factory() as session:
            operation = (
                await session.execute(
                    select(Operation).where(Operation.workspace_id == workspace_id)
                )
            ).scalar_one()
            operation_payload = dict(operation.payload or {})
        assert payload["operation_id"] == operation.id
        assert operation_payload["pending_operator_hint"]["directive"] == directive
        hint = operator_hint_from_threads(state)
        assert hint is not None
        assert hint.directive == directive
        assert hint.status == "pending"

    @pytest.mark.unit
    async def test_adoption_event_payload_records_pending_operator_hint(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        directive = "revert the workflow edit"

        workspace_id = await _adopt(factory, hint=directive)

        events = await _events(factory, workspace_id, PR_ADOPTION_REQUESTED_EVENT_TYPE)
        assert len(events) == 1
        pending = (events[0].payload or {})["pending_operator_hint"]
        assert pending["directive"] == directive
        assert pending["status"] == "pending"
        assert pending["reason_code"] == PR_ADOPTION_OPERATOR_HINT_REASON

    @pytest.mark.unit
    async def test_adoption_without_hint_omits_pending_operator_hint_from_payloads(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        workspace_id = await _adopt(factory)

        events = await _events(factory, workspace_id, PR_ADOPTION_REQUESTED_EVENT_TYPE)
        assert "pending_operator_hint" not in (events[0].payload or {})
        async with factory() as session:
            operation = (
                await session.execute(
                    select(Operation).where(Operation.workspace_id == workspace_id)
                )
            ).scalar_one()
        assert "pending_operator_hint" not in dict(operation.payload or {})

    @pytest.mark.unit
    async def test_hint_and_seeded_verdicts_coexist_on_the_new_row(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        previous_id = await _adopt(factory)
        await _fail_with_monitor_state(factory, previous_id, _SEEDABLE_PREDECESSOR_STATE)

        fresh_id = await _adopt(factory, hint="keep the deferred issues closed")

        state = await _monitor_state(factory, fresh_id)
        assert OPERATOR_HINT_STATE_KEY in state
        assert {
            key: value for key, value in state.items() if key != OPERATOR_HINT_STATE_KEY
        } == _SEEDABLE_PREDECESSOR_STATE

    @pytest.mark.unit
    async def test_seeded_hint_decides_before_unaddressed_comments(
        self,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        workspace_id = await _adopt(factory, hint="rewrite the failing migration first")

        state = await _monitor_state(factory, workspace_id)
        monitor_state = MonitorState(threads_addressed_ids=dict(state))
        monitor_state.pending_operator_hint = operator_hint_from_threads(state)
        status = PRStatus(
            number=PR_NUMBER,
            head_sha="h" * 40,
            mergeable=MergeableState.MERGEABLE,
            check_state=CheckState.SUCCESS,
            unresolved_inline_threads=(),
            unresolved_review_comments=(
                ReviewComment(comment_id="C_new", body_excerpt="please fix", author="reviewer"),
            ),
            base_behind_count=0,
            merge_state_status=MergeStateStatus.CLEAN,
        )

        action = decide(status, monitor_state, MonitorConfig(auto_merge=True))

        assert isinstance(action, AddressOperatorHint)
