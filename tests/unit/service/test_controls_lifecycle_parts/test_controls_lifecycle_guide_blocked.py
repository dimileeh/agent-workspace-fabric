"""Behavior tests for ``guide`` resolving a pre-PR ``blocked`` workspace."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.ids import new_operator_grant_id
from awf.db.enums import WorkspaceStatus
from awf.db.models import OperatorGrantAuditRecord, Workspace
from awf.runtime.operator_hints import pre_pr_operator_hint_from_payload, utcnow
from awf.service.controls import (
    IdempotencyConflictError,
    WorkspaceGuideEmptyDirectiveError,
    WorkspaceGuideGrantNotAllowedError,
    WorkspaceGuideGrantReasonRequiredError,
    WorkspaceGuideInvalidGrantPathError,
    WorkspaceGuidePolicyDowngradeRequiredError,
)
from tests.postgres import postgres_test_session
from tests.unit.service.test_controls_lifecycle_parts.controls_lifecycle_helpers import (
    _service,
    _workspace,
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_session() as s:
        yield s


async def _blocked_workspace(
    session: AsyncSession,
    *,
    block_epoch: int = 1,
    violations: list[dict[str, object]] | None = None,
) -> Workspace:
    workspace = await _workspace(session, status=WorkspaceStatus.blocked)
    workspace.block_reason_code = "QUALITY_GATE_POLICY_CHANGED"
    workspace.block_type = "protected_quality_gate"
    workspace.block_epoch = block_epoch
    workspace.block_violations = (
        violations
        if violations is not None
        else [
            {"path": "pyproject.toml", "section": "tool.coverage", "line": 5, "reason": "weakened"}
        ]
    )
    # A pre-PR blocked workspace has no PR yet.
    workspace.pr_url = None
    await session.flush()
    return workspace


async def _grants(session: AsyncSession, workspace_id: str) -> list[OperatorGrantAuditRecord]:
    rows = await session.execute(
        select(OperatorGrantAuditRecord).where(
            OperatorGrantAuditRecord.workspace_id == workspace_id
        )
    )
    return list(rows.scalars().all())


@pytest.mark.unit
async def test_guide_blocked_eligible_without_pr_url_arms_directive(
    session: AsyncSession,
) -> None:
    workspace = await _blocked_workspace(session)
    service, _stopper, _cleaner = _service(session)

    response = await service.guide_workspace(
        workspace.id,
        directive="revert the pyproject change and add a real test",
        reason="operator asked for a revert",
        idempotency_key="guide-blocked-directive",
        expected_version=workspace.version,
    )

    # No PR url required; status stays blocked (worker resume does the transition).
    assert response.status == WorkspaceStatus.blocked
    assert workspace.status == WorkspaceStatus.blocked.value
    hint = pre_pr_operator_hint_from_payload(workspace.pending_operator_hint)
    assert hint is not None
    assert hint.directive == "revert the pyproject change and add a real test"
    assert hint.status == "pending"


@pytest.mark.unit
async def test_guide_blocked_persists_epoch_scoped_grant(session: AsyncSession) -> None:
    workspace = await _blocked_workspace(session, block_epoch=3)
    service, _stopper, _cleaner = _service(session)

    await service.guide_workspace(
        workspace.id,
        directive="",
        reason="approved benign config split",
        grants=["pyproject.toml"],
        approve_policy_downgrade=True,
        operator="alice@example.com",
        idempotency_key="guide-blocked-grant",
        expected_version=workspace.version,
    )

    grants = await _grants(session, workspace.id)
    assert len(grants) == 1
    grant = grants[0]
    assert grant.normalized_path == "pyproject.toml"
    assert grant.block_epoch == 3  # scoped to the current block instance
    assert grant.approve_policy_downgrade is True
    assert grant.operator == "alice@example.com"
    assert grant.reason == "approved benign config split"
    assert grant.consumed_at is None


@pytest.mark.unit
async def test_guide_blocked_grants_only_bumps_updated_at(session: AsyncSession) -> None:
    # A grants-only approve-and-keep where no directive was ever armed clears no
    # Workspace column (``pending_operator_hint`` is already ``None``), so without
    # an explicit stamp neither the ORM ``onupdate`` hook nor
    # ``advance_workspace_version`` (which preserves ``updated_at``) would record
    # the decision. The ``updated_at``-ordered blocked-resume selector and pollers
    # that key off ``updated_at`` must still observe the grant.
    workspace = await _blocked_workspace(session, block_epoch=3)
    assert workspace.pending_operator_hint is None
    stale = utcnow() - timedelta(hours=1)
    workspace.updated_at = stale
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    await service.guide_workspace(
        workspace.id,
        directive="",
        reason="approved benign config split",
        grants=["pyproject.toml"],
        approve_policy_downgrade=True,
        operator="alice@example.com",
        idempotency_key="guide-blocked-grant-bumps-updated-at",
        expected_version=workspace.version,
    )

    assert workspace.updated_at > stale


@pytest.mark.unit
async def test_guide_blocked_grants_only_clears_stale_directive(session: AsyncSession) -> None:
    # A prior directive guide armed a revert directive on the blocked workspace.
    workspace = await _blocked_workspace(session, block_epoch=3)
    workspace.pending_operator_hint = {
        "reason": "revert it",
        "directive": "revert the pyproject change",
        "status": "pending",
        "reason_code": "OPERATOR_GUIDE",
    }
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    # The operator changes their mind and issues a grants-only approve-and-keep.
    await service.guide_workspace(
        workspace.id,
        directive="",
        reason="actually keep it",
        grants=["pyproject.toml"],
        approve_policy_downgrade=True,
        operator="alice@example.com",
        idempotency_key="guide-blocked-grant-clears-hint",
        expected_version=workspace.version,
    )

    # The stale directive must be cleared so the resume path honors the grant
    # instead of re-applying the old revert directive (which it prioritizes).
    assert pre_pr_operator_hint_from_payload(workspace.pending_operator_hint) is None
    grants = await _grants(session, workspace.id)
    assert len(grants) == 1
    assert grants[0].normalized_path == "pyproject.toml"


@pytest.mark.unit
async def test_guide_blocked_directive_only_revokes_stale_grant(session: AsyncSession) -> None:
    # A prior approve-and-keep guide recorded an active current-epoch grant.
    workspace = await _blocked_workspace(session, block_epoch=3)
    session.add(
        OperatorGrantAuditRecord(
            id=new_operator_grant_id(),
            workspace_id=workspace.id,
            operator="alice@example.com",
            reason="approved keeping it",
            normalized_path="pyproject.toml",
            block_epoch=3,
            approve_policy_downgrade=True,
            created_at=utcnow(),
        )
    )
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    # The operator changes their mind and issues a directive-only revert.
    await service.guide_workspace(
        workspace.id,
        directive="revert the pyproject change after all",
        reason="actually revert it",
        operator="alice@example.com",
        idempotency_key="guide-blocked-directive-revokes-grant",
        expected_version=workspace.version,
    )

    # The directive is armed, and the stale grant must be revoked so the resume
    # path's protected-file gates no longer honor it (mirror of the grants-only
    # branch clearing a stale directive): the latest operator decision wins.
    hint = pre_pr_operator_hint_from_payload(workspace.pending_operator_hint)
    assert hint is not None
    assert hint.directive == "revert the pyproject change after all"
    grants = await _grants(session, workspace.id)
    assert len(grants) == 1
    assert grants[0].revoked_at is not None


@pytest.mark.unit
async def test_guide_blocked_directive_with_new_grant_keeps_fresh_grant(
    session: AsyncSession,
) -> None:
    # A combined guide (grant + directive) must NOT revoke the grant it just
    # recorded in the same call — a directive supersedes PRE-EXISTING grants but
    # preserves the paths granted by this same request.
    workspace = await _blocked_workspace(
        session,
        block_epoch=2,
        violations=[{"path": "pyproject.toml", "section": "x", "line": 1, "reason": "weakened"}],
    )
    service, _stopper, _cleaner = _service(session)

    await service.guide_workspace(
        workspace.id,
        directive="also fix the unrelated module",
        reason="keep this config and fix the other file",
        grants=["docs/CONTRIBUTING.md"],
        idempotency_key="guide-blocked-directive-plus-grant",
        expected_version=workspace.version,
    )

    grants = await _grants(session, workspace.id)
    assert len(grants) == 1
    assert grants[0].normalized_path == "docs/CONTRIBUTING.md"
    assert grants[0].revoked_at is None


@pytest.mark.unit
async def test_guide_blocked_directive_with_new_grant_revokes_prior_grant(
    session: AsyncSession,
) -> None:
    # An earlier approve-and-keep guide recorded a current-epoch grant for one
    # path. Before resume, the operator sends a COMBINED directive + grant that
    # grants a DIFFERENT path. The directive is the latest decision: the prior
    # grant must be revoked (otherwise the resume path's protected gates would
    # still suppress a violation on the old path) while the freshly granted path
    # is preserved.
    workspace = await _blocked_workspace(session, block_epoch=4)
    session.add(
        OperatorGrantAuditRecord(
            id=new_operator_grant_id(),
            workspace_id=workspace.id,
            operator="alice@example.com",
            reason="approved keeping the old file",
            normalized_path="pyproject.toml",
            block_epoch=4,
            approve_policy_downgrade=True,
            created_at=utcnow(),
        )
    )
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    await service.guide_workspace(
        workspace.id,
        directive="revert pyproject and fix the other module instead",
        reason="changed my mind: revert that, grant only the new file",
        grants=["docs/CONTRIBUTING.md"],
        operator="alice@example.com",
        idempotency_key="guide-blocked-directive-revokes-prior-keeps-fresh",
        expected_version=workspace.version,
    )

    grants = await _grants(session, workspace.id)
    by_path = {grant.normalized_path: grant for grant in grants}
    # The pre-existing grant is revoked; the freshly granted path stays active.
    assert by_path["pyproject.toml"].revoked_at is not None
    assert by_path["docs/CONTRIBUTING.md"].revoked_at is None


@pytest.mark.unit
async def test_guide_blocked_weakening_grant_requires_ack(session: AsyncSession) -> None:
    workspace = await _blocked_workspace(session)
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceGuidePolicyDowngradeRequiredError):
        await service.guide_workspace(
            workspace.id,
            directive="",
            reason="please keep it",
            grants=["pyproject.toml"],  # matches a recorded violation, no ack
            idempotency_key="guide-blocked-noack",
            expected_version=workspace.version,
        )
    assert await _grants(session, workspace.id) == []


@pytest.mark.unit
async def test_guide_blocked_benign_grant_does_not_require_ack(session: AsyncSession) -> None:
    # The granted path does not match any recorded violation, so no ack needed.
    workspace = await _blocked_workspace(
        session,
        violations=[{"path": "pyproject.toml", "section": "x", "line": 1, "reason": "weakened"}],
    )
    service, _stopper, _cleaner = _service(session)

    await service.guide_workspace(
        workspace.id,
        directive="",
        reason="grant an unrelated config file",
        grants=["docs/CONTRIBUTING.md"],
        idempotency_key="guide-blocked-benign",
        expected_version=workspace.version,
    )
    grants = await _grants(session, workspace.id)
    assert len(grants) == 1
    assert grants[0].approve_policy_downgrade is False


@pytest.mark.unit
async def test_guide_blocked_non_matching_wildcard_grant_does_not_require_ack(
    session: AsyncSession,
) -> None:
    # The preflight must honor grants with the same precise membership semantics
    # as the resume-time protected gate (``_grant_matches``), not the broader
    # symmetric owned-path overlap predicate. A wildcard grant that does NOT
    # actually match the violating path (``*-lint.yml`` vs ``deploy.yml``) would
    # never suppress the violation on resume, so demanding the ack here is wrong:
    # acking it would just record a useless grant and resume would re-block.
    workspace = await _blocked_workspace(
        session,
        violations=[
            {
                "path": ".github/workflows/deploy.yml",
                "section": ".github/workflows/deploy.yml",
                "line": None,
                "reason": "weakened",
            }
        ],
    )
    service, _stopper, _cleaner = _service(session)

    await service.guide_workspace(
        workspace.id,
        directive="",
        reason="grant only the lint workflows",
        grants=[".github/workflows/*-lint.yml"],
        idempotency_key="guide-blocked-wildcard-nomatch",
        expected_version=workspace.version,
    )
    grants = await _grants(session, workspace.id)
    assert len(grants) == 1
    assert grants[0].approve_policy_downgrade is False


@pytest.mark.unit
async def test_guide_blocked_matching_wildcard_grant_requires_ack(
    session: AsyncSession,
) -> None:
    # A wildcard grant that DOES cover the violating path under precise membership
    # (``*-lint.yml`` vs ``ci-lint.yml``) would suppress the violation on resume,
    # so the policy-downgrade ack is still required up front.
    workspace = await _blocked_workspace(
        session,
        violations=[
            {
                "path": ".github/workflows/ci-lint.yml",
                "section": ".github/workflows/ci-lint.yml",
                "line": None,
                "reason": "weakened",
            }
        ],
    )
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceGuidePolicyDowngradeRequiredError):
        await service.guide_workspace(
            workspace.id,
            directive="",
            reason="please keep it",
            grants=[".github/workflows/*-lint.yml"],  # matches ci-lint.yml, no ack
            idempotency_key="guide-blocked-wildcard-match-noack",
            expected_version=workspace.version,
        )
    assert await _grants(session, workspace.id) == []


@pytest.mark.unit
async def test_guide_blocked_grant_requires_reason(session: AsyncSession) -> None:
    workspace = await _blocked_workspace(session)
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceGuideGrantReasonRequiredError):
        await service.guide_workspace(
            workspace.id,
            directive="",
            reason=None,
            grants=["pyproject.toml"],
            approve_policy_downgrade=True,
            idempotency_key="guide-blocked-noreason",
            expected_version=workspace.version,
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("./pyproject.toml", "pyproject.toml"),
        ("././src/awf/x.py", "src/awf/x.py"),
        ("  ./docs/file.md  ", "docs/file.md"),
        ("plain/path.py", "plain/path.py"),
    ],
)
def test_canonicalize_grant_path_strips_leading_dot_slash(raw: str, expected: str) -> None:
    """A grant glob's leading ``./`` segments are stripped to a repo-relative path."""
    from awf.service.controls_guide import _canonicalize_grant_path

    assert _canonicalize_grant_path(raw) == expected


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["./", "././", "   "])
def test_canonicalize_grant_path_rejects_empty_after_normalization(raw: str) -> None:
    """A glob that normalizes to nothing is rejected as an empty path."""
    from awf.service.controls_guide import _canonicalize_grant_path

    with pytest.raises(WorkspaceGuideInvalidGrantPathError, match="path is empty"):
        _canonicalize_grant_path(raw)


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad_path",
    ["../etc/passwd", "/abs/path", "a/../../b", "a/" + "b" * 1024],
)
async def test_guide_blocked_rejects_unsafe_grant_path(
    session: AsyncSession, bad_path: str
) -> None:
    workspace = await _blocked_workspace(session)
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceGuideInvalidGrantPathError):
        await service.guide_workspace(
            workspace.id,
            directive="",
            reason="trying traversal",
            grants=[bad_path],
            approve_policy_downgrade=True,
            idempotency_key=f"guide-blocked-bad-{bad_path}",
            expected_version=workspace.version,
        )


@pytest.mark.unit
async def test_guide_blocked_requires_directive_or_grant(session: AsyncSession) -> None:
    workspace = await _blocked_workspace(session)
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceGuideEmptyDirectiveError):
        await service.guide_workspace(
            workspace.id,
            directive="",
            reason="nothing to do",
            idempotency_key="guide-blocked-empty",
            expected_version=workspace.version,
        )


@pytest.mark.unit
async def test_guide_blocked_grant_same_key_different_operator_conflicts(
    session: AsyncSession,
) -> None:
    """A grant-bearing same-key retry that swaps the operator must conflict.

    The operator is persisted on ``OperatorGrantAuditRecord``; replaying the
    cached operation would silently keep the first operator's attribution, so a
    different operator on the same key must raise IDEMPOTENCY_CONFLICT instead.
    """
    workspace = await _blocked_workspace(session, block_epoch=3)
    service, _stopper, _cleaner = _service(session)

    await service.guide_workspace(
        workspace.id,
        directive="",
        reason="approved benign config split",
        grants=["pyproject.toml"],
        approve_policy_downgrade=True,
        operator="alice@example.com",
        idempotency_key="guide-blocked-operator-swap",
        expected_version=workspace.version,
    )

    # Retry under the original ``expected_version`` (the first call advanced it),
    # so the operator is the *only* differing field — proving it gates the
    # conflict rather than a version mismatch.
    with pytest.raises(IdempotencyConflictError):
        await service.guide_workspace(
            workspace.id,
            directive="",
            reason="approved benign config split",
            grants=["pyproject.toml"],
            approve_policy_downgrade=True,
            operator="mallory@example.com",
            idempotency_key="guide-blocked-operator-swap",
            expected_version=workspace.version - 1,
        )

    # The first operator's attribution is preserved (no duplicate, no overwrite).
    grants = await _grants(session, workspace.id)
    assert len(grants) == 1
    assert grants[0].operator == "alice@example.com"


@pytest.mark.unit
async def test_guide_blocked_grant_same_operator_whitespace_replays(
    session: AsyncSession,
) -> None:
    """A retry whose only operator difference is surrounding whitespace replays.

    The idempotency identity normalizes the operator the same way it is persisted
    (strip + default), so " alice "/"alice" must be treated as the same request
    rather than a spurious conflict.
    """
    workspace = await _blocked_workspace(session, block_epoch=3)
    service, _stopper, _cleaner = _service(session)

    response = await service.guide_workspace(
        workspace.id,
        directive="",
        reason="approved benign config split",
        grants=["pyproject.toml"],
        approve_policy_downgrade=True,
        operator="alice@example.com",
        idempotency_key="guide-blocked-operator-ws",
        expected_version=workspace.version,
    )

    replay = await service.guide_workspace(
        workspace.id,
        directive="",
        reason="approved benign config split",
        grants=["pyproject.toml"],
        approve_policy_downgrade=True,
        operator="  alice@example.com  ",
        idempotency_key="guide-blocked-operator-ws",
        expected_version=workspace.version - 1,
    )

    assert response.operation_id == replay.operation_id
    grants = await _grants(session, workspace.id)
    assert len(grants) == 1


@pytest.mark.unit
async def test_guide_grants_rejected_on_monitoring_pr(session: AsyncSession) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.monitoring_pr)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/9"
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceGuideGrantNotAllowedError):
        await service.guide_workspace(
            workspace.id,
            directive="do the thing",
            reason="r",
            grants=["pyproject.toml"],
            approve_policy_downgrade=True,
            idempotency_key="guide-monitoring-grant",
            expected_version=workspace.version,
        )


async def _monitor_origin_blocked_workspace(
    session: AsyncSession,
    *,
    block_epoch: int = 1,
) -> Workspace:
    """A POST-PR (monitor-origin) protected-scope block: a PR exists and the
    ``block_resume_phase`` carries the ``monitor_`` prefix that routes resume
    back into the PR monitor rather than the executor."""
    workspace = await _blocked_workspace(session, block_epoch=block_epoch)
    workspace.block_resume_phase = "monitor_protected_scope_push"
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/7"
    workspace.pr_number = 7
    await session.flush()
    return workspace


@pytest.mark.unit
async def test_guide_monitor_origin_blocked_directive_resumes_into_monitor(
    session: AsyncSession,
) -> None:
    from awf.runtime.operator_hints import operator_hint_from_threads

    workspace = await _monitor_origin_blocked_workspace(session)
    service, _stopper, _cleaner = _service(session)

    response = await service.guide_workspace(
        workspace.id,
        directive="revert the protected workflow edit",
        reason="operator asked for a revert",
        idempotency_key="guide-monitor-directive",
        expected_version=workspace.version,
    )

    # Monitor-origin block resumes back into the PR monitor, NOT the executor.
    assert response.status == WorkspaceStatus.monitoring_pr
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    # The hint is armed in the MONITOR state map (not the pre-PR column).
    hint = operator_hint_from_threads(dict(workspace.monitor_threads_addressed or {}))
    assert hint is not None
    assert hint.directive == "revert the protected workflow edit"
    assert hint.status == "pending"
    # Claims are force-cleared so the monitor poll can re-claim the row.
    assert workspace.monitor_claimed_by is None
    assert workspace.execution_claimed_by is None


@pytest.mark.unit
async def test_guide_monitor_origin_blocked_grant_only_arms_directiveless_hint(
    session: AsyncSession,
) -> None:
    from awf.runtime.operator_hints import operator_hint_from_threads

    workspace = await _monitor_origin_blocked_workspace(session, block_epoch=4)
    service, _stopper, _cleaner = _service(session)

    await service.guide_workspace(
        workspace.id,
        directive="",
        reason="approved the protected change",
        grants=["pyproject.toml"],
        approve_policy_downgrade=True,
        operator="alice@example.com",
        idempotency_key="guide-monitor-grant",
        expected_version=workspace.version,
    )

    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    # A grant-only resume still arms a (directive-less) pending hint so decide()
    # routes to AddressOperatorHint and pushes the preserved commit.
    hint = operator_hint_from_threads(dict(workspace.monitor_threads_addressed or {}))
    assert hint is not None
    assert hint.directive is None
    assert hint.status == "pending"
    grants = await _grants(session, workspace.id)
    assert len(grants) == 1
    assert grants[0].block_epoch == 4
    assert grants[0].approve_policy_downgrade is True


@pytest.mark.unit
async def test_guide_monitor_origin_blocked_directive_only_revokes_stale_grant(
    session: AsyncSession,
) -> None:
    # A prior approve-and-keep guide recorded an active current-epoch grant on a
    # POST-PR (monitor-origin) block. The operator then changes their mind and
    # issues a directive-only revert: the stale grant must be revoked so the
    # monitor hint resume's protected-scope gates (which load
    # ``_active_operator_grant_specs``) no longer suppress a violation on its path.
    workspace = await _monitor_origin_blocked_workspace(session, block_epoch=3)
    session.add(
        OperatorGrantAuditRecord(
            id=new_operator_grant_id(),
            workspace_id=workspace.id,
            operator="alice@example.com",
            reason="approved keeping it",
            normalized_path="pyproject.toml",
            block_epoch=3,
            approve_policy_downgrade=True,
            created_at=utcnow(),
        )
    )
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    await service.guide_workspace(
        workspace.id,
        directive="revert the pyproject change after all",
        reason="actually revert it",
        operator="alice@example.com",
        idempotency_key="guide-monitor-directive-revokes-grant",
        expected_version=workspace.version,
    )

    grants = await _grants(session, workspace.id)
    assert len(grants) == 1
    assert grants[0].revoked_at is not None


@pytest.mark.unit
async def test_guide_monitor_origin_blocked_directive_with_new_grant_revokes_prior(
    session: AsyncSession,
) -> None:
    # A combined directive + grant on a monitor-origin block revokes a PRE-EXISTING
    # grant (the directive is the latest decision) while preserving the path granted
    # by this same request.
    workspace = await _monitor_origin_blocked_workspace(session, block_epoch=4)
    session.add(
        OperatorGrantAuditRecord(
            id=new_operator_grant_id(),
            workspace_id=workspace.id,
            operator="alice@example.com",
            reason="approved keeping the old file",
            normalized_path="pyproject.toml",
            block_epoch=4,
            approve_policy_downgrade=True,
            created_at=utcnow(),
        )
    )
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    await service.guide_workspace(
        workspace.id,
        directive="revert pyproject and fix the other module instead",
        reason="changed my mind: revert that, grant only the new file",
        grants=["docs/CONTRIBUTING.md"],
        operator="alice@example.com",
        idempotency_key="guide-monitor-directive-revokes-prior-keeps-fresh",
        expected_version=workspace.version,
    )

    grants = await _grants(session, workspace.id)
    by_path = {grant.normalized_path: grant for grant in grants}
    assert by_path["pyproject.toml"].revoked_at is not None
    assert by_path["docs/CONTRIBUTING.md"].revoked_at is None


@pytest.mark.unit
async def test_guide_pre_pr_blocked_stays_blocked_when_not_monitor_origin(
    session: AsyncSession,
) -> None:
    """A pre-PR block (no ``monitor_`` resume phase) keeps WS-1 behavior: it stays
    ``blocked`` and arms the directive in the pre-PR column, not the monitor map."""
    workspace = await _blocked_workspace(session)
    workspace.block_resume_phase = "validating"
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    response = await service.guide_workspace(
        workspace.id,
        directive="revert it",
        reason="operator asked for a revert",
        idempotency_key="guide-pre-pr-directive",
        expected_version=workspace.version,
    )

    assert response.status == WorkspaceStatus.blocked
    assert workspace.status == WorkspaceStatus.blocked.value
    assert pre_pr_operator_hint_from_payload(workspace.pending_operator_hint) is not None
