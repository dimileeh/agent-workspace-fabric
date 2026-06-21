"""ORM tests for block-state columns and operator grant audit records."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import WorkspaceStatus
from awf.db.models import OperatorGrantAuditRecord, Workspace
from awf.db.repositories import WorkspaceRepository
from tests.postgres import postgres_test_session


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_session() as s:
        yield s


async def _workspace(session: AsyncSession) -> Workspace:
    workspace = await WorkspaceRepository(session).create(
        repo_url="git@github.com:example/grants.git",
        branch_base="main",
        task_title="operator grant test",
        task_prompt="exercise block-state columns",
        agent="codex",
        test_commands=[],
    )
    workspace.status = WorkspaceStatus.running.value
    await session.flush()
    return workspace


@pytest.mark.unit
async def test_block_state_columns_default_and_persist(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    # Defaults: block_epoch starts at 0, the rest are unset.
    assert workspace.block_epoch == 0
    assert workspace.block_reason_code is None
    assert workspace.block_violations is None
    assert workspace.pending_operator_hint is None

    workspace.status = WorkspaceStatus.blocked.value
    workspace.block_reason_code = "QUALITY_GATE_POLICY_CHANGED"
    workspace.block_type = "protected_quality_gate"
    workspace.block_violations = [
        {"path": "pyproject.toml", "section": "tool.coverage", "line": 12, "reason": "weakened"}
    ]
    workspace.block_resume_phase = "validating"
    workspace.block_epoch = 1
    workspace.blocked_at = datetime.now(UTC)
    workspace.pending_operator_hint = {"status": "pending", "directive": "revert"}
    await session.flush()
    session.expunge_all()

    reloaded = await session.get(Workspace, workspace.id)
    assert reloaded is not None
    assert reloaded.status == WorkspaceStatus.blocked.value
    assert reloaded.block_reason_code == "QUALITY_GATE_POLICY_CHANGED"
    assert reloaded.block_violations[0]["path"] == "pyproject.toml"
    assert reloaded.block_epoch == 1
    assert reloaded.pending_operator_hint == {"status": "pending", "directive": "revert"}


async def _blocked_workspace(session: AsyncSession, *, block_epoch: int = 1) -> Workspace:
    workspace = await _workspace(session)
    workspace.status = WorkspaceStatus.blocked.value
    workspace.block_epoch = block_epoch
    workspace.blocked_at = datetime.now(UTC)
    await session.flush()
    return workspace


@pytest.mark.unit
async def test_list_resumable_blocked_ids_selects_only_operator_cleared(
    session: AsyncSession,
) -> None:
    repo = WorkspaceRepository(session)

    # Awaiting an operator decision: no directive, no grant → not resumable.
    awaiting = await _blocked_workspace(session)

    # Directive armed (revert/redo) → resumable. ``guide`` always persists a
    # ``reason`` alongside the directive (``reason=reason_text or directive_text``),
    # so the realistic armed payload carries both fields.
    with_directive = await _blocked_workspace(session)
    with_directive.pending_operator_hint = {
        "status": "pending",
        "reason": "revert the protected change",
        "directive": "revert it",
    }

    # Directive present but ``reason`` missing → the resume path's
    # ``pre_pr_operator_hint_from_payload`` returns ``None`` (no reason), so it would
    # never apply the directive and the worker would re-run the agent unguided. It
    # must NOT be selected — else it spins ``blocked -> running -> blocked``.
    reasonless_directive = await _blocked_workspace(session)
    reasonless_directive.pending_operator_hint = {"status": "pending", "directive": "revert it"}

    # Directive present but ``reason`` is whitespace-only → same as missing on the
    # resume path (``reason.strip()`` is falsy) → not resumable.
    blank_reason_directive = await _blocked_workspace(session)
    blank_reason_directive.pending_operator_hint = {
        "status": "pending",
        "reason": "   ",
        "directive": "revert it",
    }

    # Directive present but ``reason`` is a NON-STRING (e.g. JSON boolean) → the
    # resume path discards a non-string reason as absent → not resumable.
    non_string_reason_directive = await _blocked_workspace(session)
    non_string_reason_directive.pending_operator_hint = {
        "status": "pending",
        "reason": True,
        "directive": "revert it",
    }

    # Hint present but carries no directive and no grant → the resume path would
    # apply neither branch, so it must NOT be selected (else blocked -> running
    # -> blocked spins re-running the same gate every cycle).
    directiveless = await _blocked_workspace(session)
    directiveless.pending_operator_hint = {"status": "pending", "reason": "paused on gate"}

    # Hint with a whitespace-only directive is treated as directive-less by the
    # resume path (``_optional_stripped_string``) → also not resumable.
    blank_directive = await _blocked_workspace(session)
    blank_directive.pending_operator_hint = {
        "status": "pending",
        "reason": "paused on gate",
        "directive": "   ",
    }

    # Hint with a NON-STRING directive (e.g. a JSON boolean): the resume path's
    # ``_optional_stripped_string`` discards any non-string value as absent, so a
    # directive that merely coerces to non-empty text must NOT clear eligibility
    # here — otherwise it reproduces the blocked -> running -> blocked spin loop.
    non_string_directive = await _blocked_workspace(session)
    non_string_directive.pending_operator_hint = {
        "status": "pending",
        "reason": "paused on gate",
        "directive": True,
    }

    # Active grant for the current epoch (approve-and-keep) → resumable.
    with_grant = await _blocked_workspace(session, block_epoch=2)
    session.add(
        OperatorGrantAuditRecord(
            id="grant_active",
            workspace_id=with_grant.id,
            operator="op@example.com",
            reason="approved",
            normalized_path="pyproject.toml",
            block_epoch=2,
        )
    )

    # Grant exists but is stale (consumed) or scoped to a prior epoch → not resumable.
    with_stale_grant = await _blocked_workspace(session, block_epoch=3)
    session.add(
        OperatorGrantAuditRecord(
            id="grant_consumed",
            workspace_id=with_stale_grant.id,
            operator="op@example.com",
            reason="approved",
            normalized_path="pyproject.toml",
            block_epoch=3,
            consumed_at=datetime.now(UTC),
        )
    )
    session.add(
        OperatorGrantAuditRecord(
            id="grant_wrong_epoch",
            workspace_id=with_stale_grant.id,
            operator="op@example.com",
            reason="approved",
            normalized_path="pyproject.toml",
            block_epoch=2,
        )
    )

    # A directive on a non-blocked workspace is ignored (status gate).
    running = await _workspace(session)
    running.pending_operator_hint = {"status": "pending", "directive": "revert it"}

    # A MONITOR-ORIGIN block (post-PR) carries an actionable directive AND an
    # active grant, but it is resumed via ``monitoring_pr`` (the guide control
    # transitions it back into the monitor), NOT through the executor's
    # ``blocked -> running`` path. The ``monitor_`` resume-phase prefix marks it,
    # so the executor blocked-resume selector must EXCLUDE it — otherwise the
    # executor and monitor would race to resume the same row.
    monitor_origin = await _blocked_workspace(session, block_epoch=4)
    monitor_origin.block_resume_phase = "monitor_protected_scope_push"
    monitor_origin.pending_operator_hint = {
        "status": "pending",
        "reason": "revert the protected change",
        "directive": "revert it",
    }
    session.add(
        OperatorGrantAuditRecord(
            id="grant_monitor_origin",
            workspace_id=monitor_origin.id,
            operator="op@example.com",
            reason="approved",
            normalized_path="pyproject.toml",
            block_epoch=4,
        )
    )

    await session.flush()

    ids = await repo.list_resumable_blocked_ids(limit=10)
    assert set(ids) == {with_directive.id, with_grant.id}
    assert awaiting.id not in ids
    assert reasonless_directive.id not in ids
    assert blank_reason_directive.id not in ids
    assert non_string_reason_directive.id not in ids
    assert directiveless.id not in ids
    assert blank_directive.id not in ids
    assert monitor_origin.id not in ids
    assert non_string_directive.id not in ids
    assert with_stale_grant.id not in ids
    assert running.id not in ids

    # exclude_ids drops already-tracked workspaces; limit bounds the batch.
    excluded = await repo.list_resumable_blocked_ids(limit=10, exclude_ids={with_directive.id})
    assert set(excluded) == {with_grant.id}
    assert len(await repo.list_resumable_blocked_ids(limit=1)) == 1
    assert await repo.list_resumable_blocked_ids(limit=0) == []


@pytest.mark.unit
async def test_list_resumable_blocked_ids_monitor_prefix_underscore_is_literal(
    session: AsyncSession,
) -> None:
    """The monitor-origin exclusion must treat ``monitor_`` as a literal prefix.

    ``_`` is a single-char LIKE wildcard, so a naive ``LIKE 'monitor_%'`` filter is
    broader than the Python ``is_monitor_origin_block_resume_phase`` discriminator
    (a literal ``startswith('monitor_')``). A pre-PR executor phase that merely
    starts with ``monitor`` followed by a non-underscore char (e.g. ``monitorX``)
    is NOT monitor-origin and must stay eligible for executor resume — otherwise
    the loose wildcard would wrongly drop it.
    """
    repo = WorkspaceRepository(session)
    armed_hint = {
        "status": "pending",
        "reason": "revert the protected change",
        "directive": "revert it",
    }

    # Phase starts with ``monitor`` + a non-underscore char: NOT monitor-origin.
    pre_pr_lookalike = await _blocked_workspace(session)
    pre_pr_lookalike.block_resume_phase = "monitorX_phase"
    pre_pr_lookalike.pending_operator_hint = dict(armed_hint)

    # A genuine monitor-origin phase (literal ``monitor_`` prefix) stays excluded.
    monitor_origin = await _blocked_workspace(session)
    monitor_origin.block_resume_phase = "monitor_protected_scope_push"
    monitor_origin.pending_operator_hint = dict(armed_hint)

    await session.flush()

    ids = set(await repo.list_resumable_blocked_ids(limit=10))
    assert pre_pr_lookalike.id in ids
    assert monitor_origin.id not in ids


@pytest.mark.unit
async def test_list_resumable_blocked_ids_scopes_to_owning_node(
    session: AsyncSession,
) -> None:
    """A blocked workspace's warm stack lives on the node that ran it.

    When ``node_id`` is supplied the query must only surface rows owned by that
    node (or NULL/legacy rows adoptable anywhere) — otherwise a worker on
    another node would claim a blocked workspace and resume against a compose
    stack/worktree that only exists elsewhere.
    """
    repo = WorkspaceRepository(session)
    armed_hint = {
        "status": "pending",
        "reason": "revert the protected change",
        "directive": "revert it",
    }

    on_node_a = await _blocked_workspace(session)
    on_node_a.node_id = "node-a"
    on_node_a.pending_operator_hint = dict(armed_hint)

    on_node_b = await _blocked_workspace(session)
    on_node_b.node_id = "node-b"
    on_node_b.pending_operator_hint = dict(armed_hint)

    # NULL/legacy node is adoptable by any node.
    unstamped = await _blocked_workspace(session)
    unstamped.pending_operator_hint = dict(armed_hint)

    await session.flush()

    # Scoped to node-a: only node-a's row plus the unstamped one.
    assert set(await repo.list_resumable_blocked_ids(limit=10, node_id="node-a")) == {
        on_node_a.id,
        unstamped.id,
    }
    # Scoped to node-b: only node-b's row plus the unstamped one.
    assert set(await repo.list_resumable_blocked_ids(limit=10, node_id="node-b")) == {
        on_node_b.id,
        unstamped.id,
    }
    # No node scope (default): every resumable row regardless of owning node.
    assert set(await repo.list_resumable_blocked_ids(limit=10)) == {
        on_node_a.id,
        on_node_b.id,
        unstamped.id,
    }


@pytest.mark.unit
async def test_list_resumable_blocked_ids_sqlite_validates_directive_string_type() -> None:
    """The SQLite eligibility branch must type-check the directive via json_type.

    ``db/session.make_engine`` enforces Postgres, so the SQLite dialect branch is
    exercised by compiling the statement rather than executing it. The directive
    must be gated on ``json_type(...) = 'text'`` so a non-string directive cannot
    clear eligibility, mirroring the resume path's ``_optional_stripped_string``.
    """

    class _EmptyResult:
        def scalars(self) -> _EmptyResult:
            return self

        def all(self) -> list[str]:
            return []

    class _RecordingSession:
        info: dict[str, str] = {}
        bind = None

        def __init__(self) -> None:
            self.executed: list[object] = []

        async def execute(self, statement: object) -> _EmptyResult:
            self.executed.append(statement)
            return _EmptyResult()

    recording_session = _RecordingSession()
    repo = WorkspaceRepository(recording_session, dialect_name="sqlite")  # type: ignore[arg-type]

    result = await repo.list_resumable_blocked_ids(limit=10)

    assert result == []
    assert len(recording_session.executed) == 1
    sql = " ".join(
        str(
            recording_session.executed[0].compile(  # type: ignore[attr-defined]
                dialect=sqlite.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).split()
    )
    assert "json_type(workspaces.pending_operator_hint, '$.directive') = 'text'" in sql
    assert "json_extract(workspaces.pending_operator_hint, '$.directive')" in sql
    # The resume path also requires a non-blank string ``reason``; eligibility
    # must mirror that so a reason-less directive cannot be selected.
    assert "json_type(workspaces.pending_operator_hint, '$.reason') = 'text'" in sql
    assert "json_extract(workspaces.pending_operator_hint, '$.reason')" in sql


async def _recovering_workspace(
    session: AsyncSession,
    *,
    not_before: datetime | None,
) -> Workspace:
    workspace = await _workspace(session)
    workspace.status = WorkspaceStatus.recovering.value
    state: dict[str, object] = {"action": "retry", "retry_attempt_number": 1}
    if not_before is not None:
        state["not_before"] = not_before.isoformat()
    workspace.task_policy = {"provider_recovery_state": state}
    await session.flush()
    return workspace


@pytest.mark.unit
async def test_list_resumable_recovering_ids_cooldown_gate(session: AsyncSession) -> None:
    """Only ``recovering`` rows whose provider cooldown has elapsed are selected (#612)."""
    repo = WorkspaceRepository(session)
    now = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)

    cooled = await _recovering_workspace(session, not_before=now - timedelta(seconds=1))
    # Still inside the cooldown → excluded (the worker must not resume early).
    await _recovering_workspace(session, not_before=now + timedelta(seconds=300))
    # No cooldown deadline at all → excluded (left to stale-active recovery).
    await _recovering_workspace(session, not_before=None)
    # A different non-terminal status is never a recovering-resume candidate.
    running = await _workspace(session)
    running.task_policy = {
        "provider_recovery_state": {"not_before": (now - timedelta(seconds=10)).isoformat()}
    }
    await session.flush()

    selected = await repo.list_resumable_recovering_ids(limit=10, now=now)
    assert selected == [cooled.id]


@pytest.mark.unit
async def test_list_resumable_recovering_ids_scopes_to_owning_node(session: AsyncSession) -> None:
    """A recovering workspace's warm stack lives on the node that ran it."""
    repo = WorkspaceRepository(session)
    now = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
    elapsed = now - timedelta(seconds=5)

    on_node_a = await _recovering_workspace(session, not_before=elapsed)
    on_node_a.node_id = "node-a"
    on_node_b = await _recovering_workspace(session, not_before=elapsed)
    on_node_b.node_id = "node-b"
    unstamped = await _recovering_workspace(session, not_before=elapsed)
    await session.flush()

    assert set(await repo.list_resumable_recovering_ids(limit=10, now=now, node_id="node-a")) == {
        on_node_a.id,
        unstamped.id,
    }
    assert set(await repo.list_resumable_recovering_ids(limit=10, now=now)) == {
        on_node_a.id,
        on_node_b.id,
        unstamped.id,
    }
    # ``exclude_ids`` drops in-flight rows; a non-positive limit short-circuits.
    assert unstamped.id not in await repo.list_resumable_recovering_ids(
        limit=10, now=now, exclude_ids={unstamped.id}
    )
    assert await repo.list_resumable_recovering_ids(limit=0, now=now) == []


@pytest.mark.unit
async def test_list_resumable_recovering_ids_accepts_z_suffixed_not_before(
    session: AsyncSession,
) -> None:
    """A ``Z``-suffixed UTC deadline still orders chronologically (TIMESTAMPTZ cast).

    Raw ISO-string comparison would mis-rank ``...:00Z`` against the
    ``...:00.<us>+00:00`` form of ``now`` (``'Z' > '.'`` / ``'Z' > '+'`` in ASCII)
    and strand the elapsed row in ``recovering`` forever; the PostgreSQL cast to
    ``TIMESTAMPTZ`` keeps the gate correct regardless of offset spelling.
    """
    repo = WorkspaceRepository(session)
    now = datetime(2026, 6, 20, 12, 0, 0, 500000, tzinfo=UTC)

    elapsed_z = await _workspace(session)
    elapsed_z.status = WorkspaceStatus.recovering.value
    # Half a second in the past, but written with a ``Z`` suffix (not isoformat()):
    # lexically "2026-06-20T12:00:00Z" > "2026-06-20T12:00:00.500000+00:00".
    elapsed_z.task_policy = {
        "provider_recovery_state": {
            "action": "retry",
            "retry_attempt_number": 1,
            "not_before": "2026-06-20T12:00:00Z",
        }
    }
    await session.flush()

    selected = await repo.list_resumable_recovering_ids(limit=10, now=now)
    assert selected == [elapsed_z.id]


@pytest.mark.unit
async def test_list_resumable_recovering_ids_sqlite_extracts_not_before() -> None:
    """The SQLite cooldown branch extracts ``not_before`` via ``json_extract``.

    ``db/session.make_engine`` enforces Postgres, so the SQLite dialect branch is
    exercised by compiling the statement rather than executing it.
    """

    class _EmptyResult:
        def scalars(self) -> _EmptyResult:
            return self

        def all(self) -> list[str]:
            return []

    class _RecordingSession:
        info: dict[str, str] = {}
        bind = None

        def __init__(self) -> None:
            self.executed: list[object] = []

        async def execute(self, statement: object) -> _EmptyResult:
            self.executed.append(statement)
            return _EmptyResult()

    recording_session = _RecordingSession()
    repo = WorkspaceRepository(recording_session, dialect_name="sqlite")  # type: ignore[arg-type]

    result = await repo.list_resumable_recovering_ids(
        limit=10, now=datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
    )

    assert result == []
    assert len(recording_session.executed) == 1
    sql = " ".join(
        str(
            recording_session.executed[0].compile(  # type: ignore[attr-defined]
                dialect=sqlite.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).split()
    )
    assert "json_extract(workspaces.task_policy, '$.provider_recovery_state.not_before')" in sql


@pytest.mark.unit
async def test_operator_grant_audit_record_persists_and_cascades(session: AsyncSession) -> None:
    workspace = await _workspace(session)
    grant = OperatorGrantAuditRecord(
        id="grant_1",
        workspace_id=workspace.id,
        operator="alice@example.com",
        reason="approved benign dependency bump",
        normalized_path="pyproject.toml",
        block_epoch=1,
        approve_policy_downgrade=False,
    )
    session.add(grant)
    await session.flush()
    session.expunge_all()

    reloaded = await session.get(OperatorGrantAuditRecord, "grant_1")
    assert reloaded is not None
    assert reloaded.operator == "alice@example.com"
    assert reloaded.block_epoch == 1
    assert reloaded.approve_policy_downgrade is False
    # Unconsumed + unrevoked grant is active for its epoch.
    assert reloaded.is_active_for_epoch is True

    reloaded.consumed_at = datetime.now(UTC)
    await session.flush()
    assert reloaded.is_active_for_epoch is False

    # Cascade delete-orphan: removing the workspace removes its grants.
    ws = await session.get(Workspace, workspace.id)
    assert ws is not None
    await session.delete(ws)
    await session.flush()
    remaining = (
        await session.execute(
            select(OperatorGrantAuditRecord).where(OperatorGrantAuditRecord.id == "grant_1")
        )
    ).scalar_one_or_none()
    assert remaining is None
