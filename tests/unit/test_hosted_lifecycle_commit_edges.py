"""Final transactional rejection paths complement actual HTTP/PG race tests."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from awf.hosted_node import lifecycle_operations as operations
from awf.hosted_node import lifecycle_reauthorization as reauthorization
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.runtime_schemas import (
    HostedControlAuthorization,
    HostedReauthorization,
    HostedWorkspace,
)
from tests.unit.test_hosted_runtime_contract import hosted_create


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "phase",
        "key",
        "quarantine",
        "grant",
        "revoked",
        "receipt",
        "state",
        "superseded",
        "scope",
        "revision",
        "fence",
        "expired",
        "file",
    ],
)
async def test_final_material_lock_rejects_each_authority_loss(monkeypatch, fault):
    raw = hosted_create()["hosted"]
    for binding in raw["credentials"].values():
        binding["revision"] = binding["fence"] = 2
    association = HostedWorkspace.model_validate(raw)
    gate = SimpleNamespace(phase="ready", recovery_key="accepted", restore_quarantine=None)
    if fault == "phase":
        gate.phase = "draining"
    elif fault == "key":
        gate.recovery_key = None
    elif fault == "quarantine":
        gate.restore_quarantine = {"state": "held"}
    monkeypatch.setattr(operations, "locked", AsyncMock(return_value=gate))
    grant = SimpleNamespace(current="receipt", revoked=fault == "revoked")
    receiver = SimpleNamespace(
        identity=association.credentials.agent.identity,
        expected=Mock(),
        preparation=AsyncMock(return_value=SimpleNamespace(id="preparation")),
        grant=AsyncMock(return_value=None if fault == "grant" else grant),
        superseded=AsyncMock(return_value=fault == "superseded"),
        store=SimpleNamespace(exists=Mock(return_value=fault != "file")),
    )
    authority = SimpleNamespace(receiver=receiver, guard_new_workspace=AsyncMock())
    bindings = iter((association.credentials.agent, association.credentials.github))

    async def receipt(*args):
        raw = next(bindings).model_dump(mode="json")
        if fault in {"revision", "fence"}:
            raw[fault] -= 1
        elif fault == "scope":
            raw["namespace"] = "foreign"
        elif fault == "expired":
            raw["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        return (
            None
            if fault == "receipt"
            else SimpleNamespace(
                id="receipt", binding=raw, state="expired" if fault == "state" else "installed"
            )
        )

    session = SimpleNamespace(info={}, get=AsyncMock(side_effect=receipt))
    if fault is None:
        await operations.lock_materials(session, authority, association, new=True)
        authority.guard_new_workspace.assert_awaited_once_with(session)
        assert receiver.expected.call_count == receiver.store.exists.call_count == 2
        assert session.info["awf_hosted_lifecycle_expiry"] == min(
            association.credentials.agent.expires_at, association.credentials.github.expires_at
        )
        bindings = iter((association.credentials.agent, association.credentials.github))
        authority.guard_new_workspace.reset_mock()
        await operations.lock_materials(session, authority, association)
        authority.guard_new_workspace.assert_not_awaited()
    else:
        with pytest.raises(MaterialDeniedError):
            await operations.lock_materials(session, authority, association, new=True)
        assert not session.info


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "current",
        "ack",
        "gate",
        "identity",
        "phase",
        "key",
        "quarantine",
        "config",
        "disk",
        "database",
        "acceptance",
        "evidence",
        "prep_owner",
        "namespace",
        "workspace",
        "preparation",
        "binding_identity",
        "target",
        "purpose",
        "grant",
        "receipt_binding",
        "operation",
        "state",
    ],
)
async def test_historical_proof_requires_every_durable_binding(monkeypatch, fault):
    association = HostedWorkspace.model_validate(hosted_create()["hosted"])
    previous = association.credentials.agent.identity
    current = previous.model_copy(update={"boot_epoch": previous.boot_epoch + 2})
    if fault == "current":
        current = previous
    owner = SimpleNamespace(acknowledgement=None if fault == "ack" else "identity-ack")
    monkeypatch.setattr(reauthorization, "check", AsyncMock(return_value=owner))
    gate = SimpleNamespace(
        identity=current.model_dump(),
        phase="ready",
        recovery_key="accepted",
        receipt_ref="receipt",
        previous_identity=previous.model_dump(),
        disk_ref="disk",
        database_ref="database",
        restore_quarantine=None,
        restore_reconciliation_id=None,
    )
    gate.recovery_acceptance = {
        key: getattr(gate, field)
        for key, field in {
            "idempotency_key": "recovery_key",
            "receipt_ref": "receipt_ref",
            "disk_ref": "disk_ref",
            "database_ref": "database_ref",
            "previous_identity": "previous_identity",
        }.items()
    }
    if fault == "identity":
        gate.identity = previous.model_dump()
    elif fault == "phase":
        gate.phase = "recovering"
    elif fault == "key":
        gate.recovery_key = None
    elif fault == "quarantine":
        gate.restore_quarantine = {"state": "held"}
    elif fault in {"disk", "database"}:
        setattr(gate, fault + "_ref", "foreign")
    elif fault == "acceptance":
        gate.recovery_acceptance = None
    authority = SimpleNamespace(
        receiver=SimpleNamespace(
            identity=current,
            config=SimpleNamespace(
                lifecycle=None
                if fault == "config"
                else SimpleNamespace(disk_ref="disk", database_ref="database")
            ),
        )
    )
    bindings = iter((association.credentials.agent, association.credentials.github))

    async def evidence(*args):
        binding = next(bindings)
        prep = SimpleNamespace(
            owner=current.owner,
            namespace=binding.namespace,
            workspace_id=association.workspace_id,
            preparation_id=binding.preparation_id,
            binding=binding.model_dump(mode="json"),
        )
        grant = SimpleNamespace(grant_id=binding.grant_id)
        receipt = SimpleNamespace(
            binding=binding.model_dump(mode="json"), operation="install", state="installed"
        )
        if fault in {"prep_owner", "namespace", "workspace", "preparation"}:
            setattr(
                prep,
                {
                    "prep_owner": "owner",
                    "workspace": "workspace_id",
                    "preparation": "preparation_id",
                }.get(fault, fault),
                "foreign",
            )
        elif fault in {"binding_identity", "target", "purpose"}:
            prep.binding["identity" if fault == "binding_identity" else fault] = "foreign"
        elif fault == "grant":
            grant.grant_id = "foreign"
        elif fault == "receipt_binding":
            receipt.binding = {}
        elif fault in {"operation", "state"}:
            setattr(receipt, fault, "revoked")
        return SimpleNamespace(
            one_or_none=lambda: None if fault == "evidence" else (prep, grant, receipt)
        )

    session = SimpleNamespace(
        get=AsyncMock(return_value=None if fault == "gate" else gate),
        execute=AsyncMock(side_effect=evidence),
    )
    if fault in (None, "current"):
        await reauthorization.historical_evidence(session, authority, association)
        assert session.execute.await_count == 2
    else:
        with pytest.raises(MaterialDeniedError):
            await reauthorization.historical_evidence(session, authority, association)


@pytest.mark.parametrize("fault", ["missing", "state", "owner"])
async def test_rewind_boundary_requires_a_committed_same_owner_journal(fault):
    source = HostedWorkspace.model_validate(hosted_create()["hosted"]).credentials.agent.identity
    journal = SimpleNamespace(state="committed", owner=source.owner, identity=source.model_dump())
    if fault == "state":
        journal.state = "staged"
    elif fault == "owner":
        journal.owner = "foreign"
    session = SimpleNamespace(get=AsyncMock(return_value=None if fault == "missing" else journal))
    with pytest.raises(MaterialDeniedError, match="HOSTED_WORKSPACE_HISTORY_MISMATCH"):
        await reauthorization.require_nonrewound_history(
            session, SimpleNamespace(restore_reconciliation_id="journal"), source
        )


@pytest.mark.parametrize("state", ["expected", "replacement", "initial-conflict", "raced-conflict"])
async def test_rebinding_rechecks_locked_preimage_before_history_or_operation(monkeypatch, state):
    raw = hosted_create()["hosted"]
    expected = HostedWorkspace.model_validate(raw)
    for binding in raw["credentials"].values():
        for key in ("grant_id", "preparation_id", "idempotency_key"):
            binding[key] = "fresh-" + binding[key]
    replacement = HostedWorkspace.model_validate(raw)
    pair = HostedReauthorization(expected_association=expected, replacement_association=replacement)
    stored = replacement if state == "replacement" else expected
    row = SimpleNamespace(
        id=stored.workspace_id,
        agent="codex",
        node_id=stored.credentials.agent.identity.owner,
        task_policy={"hosted_node": stored.model_dump(mode="json")},
    )
    foreign = expected.model_dump(mode="json")
    foreign["credentials"]["agent"]["preparation_id"] = "different-history"
    if state == "initial-conflict":
        row.task_policy = {"hosted_node": foreign}

    async def refresh(*args, **kwargs):
        if state == "raced-conflict":
            row.task_policy = {"hosted_node": foreign}

    session = SimpleNamespace(
        get=AsyncMock(return_value=row), refresh=AsyncMock(side_effect=refresh)
    )
    authority = SimpleNamespace(
        receiver=SimpleNamespace(identity=stored.credentials.agent.identity), materials=AsyncMock()
    )
    lock, quiet, history = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(reauthorization, "lock_materials", lock)
    monkeypatch.setattr(reauthorization, "require_rebind_quiet", quiet)
    monkeypatch.setattr(reauthorization, "historical_evidence", history)
    call = reauthorization.authorize_rebinding(
        session,
        authority,
        row.id,
        HostedControlAuthorization(namespace="tenant", actor="caller"),
        pair,
    )
    if "conflict" in state:
        with pytest.raises(MaterialDeniedError, match="HOSTED_REAUTHORIZATION_PREIMAGE_MISMATCH"):
            await call
        history.assert_not_awaited()
        if state == "initial-conflict":
            authority.materials.assert_not_awaited()
        else:
            session.refresh.assert_awaited_once_with(row, with_for_update=True)
    else:
        await call
        authority.materials.assert_awaited_once_with(replacement, "codex", current=True)
        lock.assert_awaited_once_with(session, authority, replacement)
        assert quiet.await_count == history.await_count == (1 if state == "expected" else 0)


@pytest.mark.parametrize("historical", [False, True])
async def test_source_history_calls_quiescence_and_evidence_only_for_old_identity(
    monkeypatch, historical
):
    stored = HostedWorkspace.model_validate(hosted_create()["hosted"])
    identity = stored.credentials.agent.identity
    if historical:
        identity = identity.model_copy(update={"boot_epoch": identity.boot_epoch + 2})
    row = SimpleNamespace(
        id=stored.workspace_id,
        task_policy={"hosted_node": stored.model_dump(mode="json")},
        node_id=identity.owner,
    )
    authority = SimpleNamespace(receiver=SimpleNamespace(identity=identity))
    quiet, proof = AsyncMock(), AsyncMock()
    monkeypatch.setattr(reauthorization, "require_source_quiet", quiet)
    monkeypatch.setattr(reauthorization, "historical_evidence", proof)
    session = object()
    assert await reauthorization.historical_source(session, authority, row, "tenant") == stored
    assert quiet.await_count == proof.await_count == int(historical)


@pytest.mark.parametrize(
    "state", ["terminal", "warm", "old-warm", "execution", "monitor", "operation"]
)
async def test_quiet_rebind_never_promotes_claimed_or_old_boot_warm_work(monkeypatch, state):
    stored = HostedWorkspace.model_validate(hosted_create()["hosted"])
    identity = stored.credentials.agent.identity
    if state == "old-warm":
        identity = identity.model_copy(update={"boot_epoch": identity.boot_epoch + 1})
    authority = SimpleNamespace(receiver=SimpleNamespace(identity=identity))
    row = SimpleNamespace(
        id=stored.workspace_id,
        status="failed" if state == "terminal" else "blocked",
        execution_claimed_by="worker" if state == "execution" else None,
        monitor_claimed_by="monitor" if state == "monitor" else None,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value="unknown" if state == "operation" else None)
    )
    terminal = AsyncMock()
    monkeypatch.setattr(reauthorization, "require_source_quiet", terminal)
    call = reauthorization.require_rebind_quiet(session, authority, row, stored)
    if state not in {"warm", "terminal"}:
        with pytest.raises(MaterialDeniedError):
            await call
        terminal.assert_not_awaited()
    else:
        await call
        assert terminal.await_count == int(state == "terminal")


@pytest.mark.parametrize("exists", [False, True])
async def test_new_destination_checks_id_before_node_lock(monkeypatch, exists):
    association = HostedWorkspace.model_validate(hosted_create()["hosted"])
    session = SimpleNamespace(
        execute=AsyncMock(), get=AsyncMock(return_value=object() if exists else None)
    )
    authority, lock = object(), AsyncMock()
    monkeypatch.setattr(operations, "lock_materials", lock)
    if exists:
        with pytest.raises(MaterialDeniedError, match="HOSTED_WORKSPACE_ID_CONFLICT"):
            await operations.new_destination(session, authority, association)
        lock.assert_not_awaited()
    else:
        await operations.new_destination(session, authority, association)
        lock.assert_awaited_once_with(session, authority, association, new=True)
    assert "pg_advisory_xact_lock" in str(session.execute.call_args.args[0])
    assert session.execute.call_args.args[1] == {"workspace_id": association.workspace_id}


@pytest.mark.parametrize("mismatch", [False, True])
async def test_task_join_rejects_historical_foreign_or_attention_workspace(mismatch):
    association = HostedWorkspace.model_validate(hosted_create()["hosted"])
    session = SimpleNamespace(scalar=AsyncMock(return_value="foreign" if mismatch else None))
    if mismatch:
        with pytest.raises(MaterialDeniedError, match="HOSTED_TASK_BINDING_MISMATCH"):
            await operations.check_task_join(session, "task", association)
    else:
        await operations.check_task_join(session, "task", association)
    query = str(session.scalar.call_args.args[0])
    assert "task_attempts" in query and "IS DISTINCT FROM" in query


@pytest.mark.parametrize("fault", [None, "status", "claim", "unreleased"])
async def test_terminal_source_requires_actual_release_and_no_claim(monkeypatch, fault):
    row = SimpleNamespace(
        status="running" if fault == "status" else "failed",
        execution_claimed_by="worker" if fault == "claim" else None,
        monitor_claimed_by=None,
    )
    release = AsyncMock(return_value=fault == "unreleased")
    monkeypatch.setattr(
        "awf.service.workspaces_retry_runtime._source_runtime_not_yet_released", release
    )
    if fault:
        with pytest.raises(MaterialDeniedError, match="HOSTED_SOURCE_NOT_QUIESCENT"):
            await operations.require_source_quiet(object(), row)
    else:
        await operations.require_source_quiet(object(), row)
        release.assert_awaited_once()


@pytest.mark.parametrize(
    "url", [None, "https://github.com/foreign/repo.git", "https://github.com/EXAMPLE/REPO.git"]
)
async def test_attention_lookup_closes_stream_and_matches_exact_repo(url):
    from awf.common.github_client import RepoRef

    class Rows:
        def __init__(self):
            self.rows = iter([] if url is None else [url])
            self.close = AsyncMock()

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.rows)
            except StopIteration:
                raise StopAsyncIteration from None

    rows = Rows()
    session = SimpleNamespace(stream_scalars=AsyncMock(return_value=rows))
    call = operations.reject_pr_attention(
        session, RepoRef.from_url("https://github.com/example/repo.git"), 42
    )
    if url and "EXAMPLE" in url:
        with pytest.raises(MaterialDeniedError, match="RECOVERY_ATTENTION_REQUIRED"):
            await call
    else:
        await call
    rows.close.assert_awaited_once()


async def test_local_commit_is_not_hosted_and_stored_hosted_row_cannot_bypass_authority():
    session = SimpleNamespace(
        info={},
        commit=AsyncMock(),
        get=AsyncMock(
            return_value=SimpleNamespace(task_policy={"hosted_node": {"workspace_id": "ws_hosted"}})
        ),
    )
    await operations.commit_hosted(session)
    session.commit.assert_not_awaited()
    with pytest.raises(MaterialDeniedError, match="HOSTED_RUNTIME_INTEGRATION_REQUIRED"):
        await operations.control_authority(session, "ws_hosted", None)


@pytest.mark.parametrize("change", [False, True, "pair"])
async def test_existing_control_rechecks_source_after_material_lock(monkeypatch, change):
    stored = HostedWorkspace.model_validate(hosted_create()["hosted"])
    row = SimpleNamespace(
        id=stored.workspace_id,
        node_id=stored.credentials.agent.identity.owner,
        agent="codex",
        task_policy={"hosted_node": stored.model_dump(mode="json")},
    )
    authority = SimpleNamespace(
        receiver=SimpleNamespace(identity=stored.credentials.agent.identity), materials=AsyncMock()
    )

    async def refresh(*args, **kwargs):
        if change is True:
            raw = stored.model_dump(mode="json")
            raw["credentials"]["agent"]["preparation_id"] = "raced"
            row.task_policy = {"hosted_node": raw}

    session = SimpleNamespace(
        get=AsyncMock(return_value=row), refresh=AsyncMock(side_effect=refresh)
    )
    lock, rebind = AsyncMock(), AsyncMock()
    monkeypatch.setattr(operations, "authority_for", lambda *_args: authority)
    monkeypatch.setattr(operations, "lock_materials", lock)
    monkeypatch.setattr(reauthorization, "authorize_rebinding", rebind)
    authorization = HostedControlAuthorization(namespace="tenant", actor="caller")
    pair = object() if change == "pair" else None
    call = operations.control_authority(session, row.id, authorization, reauthorization=pair)
    if change is True:
        with pytest.raises(MaterialDeniedError, match="HOSTED_WORKSPACE_BINDING_MISMATCH"):
            await call
    else:
        await call
    if pair:
        rebind.assert_awaited_once_with(session, authority, row.id, authorization, pair)
        session.get.assert_not_awaited()
    else:
        lock.assert_awaited_once_with(session, authority, stored)
        session.refresh.assert_awaited_once_with(row, with_for_update=True)
