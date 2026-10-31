"""Direct renewal calls exercise durable caps without ASGI dispatch indirection."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.models import Grant, Receipt
from awf.hosted_node.schemas import Binding, PreparationRenewal

from .test_preparation_renewal import clock as clock
from .test_preparation_renewal import durable, installed, renewal


async def test_direct_renewal_is_metadata_only_and_idempotent(hosted, clock):
    _, receiver, _ = hosted
    prep, binding, slot = await installed(hosted, clock)
    request = PreparationRenewal.model_validate(renewal(prep, binding, clock))
    before = await durable(receiver)
    files = {path: path.read_bytes() for path in receiver.store.root.glob("*/*.material")}
    accepted = await receiver.renew_preparation(request)
    after = await durable(receiver)
    assert accepted.expires_at == request.expires_at
    assert after[0]["expires_at"] == accepted.model_dump(mode="json")["expires_at"]
    assert after[1] == before[1] and after[2][slot] == before[2][slot]
    assert after[2][accepted.receipt_id][0:2] == ("prepare_renew", "renewed")
    assert after[2][accepted.receipt_id][3] == slot
    assert {path: path.read_bytes() for path in files} == files
    assert await receiver.renew_preparation(request) == accepted
    assert await durable(receiver) == after
    with pytest.raises(MaterialDeniedError, match="IDEMPOTENCY_CONFLICT"):
        await receiver.renew_preparation(
            request.model_copy(update={"expires_at": request.expires_at + timedelta(seconds=1)})
        )
    assert await durable(receiver) == after


@pytest.mark.parametrize("fault", ["same", "old-cap", "receipt-state", "grant-version", "binding"])
async def test_direct_renewal_conflict_preserves_cap_and_receipt(hosted, clock, fault):
    _, receiver, _ = hosted
    prep, binding, slot = await installed(hosted, clock)
    request = PreparationRenewal.model_validate(renewal(prep, binding, clock))
    reason = "GRANT_CONFLICT"
    if fault == "same":
        request = request.model_copy(update={"expires_at": request.expected_preparation_expires_at})
        reason = "INVALID_EXPIRY"
    elif fault == "old-cap":
        request = request.model_copy(update={"expected_preparation_expires_at": request.expires_at})
        reason = "PREPARATION_CONFLICT"
    elif fault == "binding":
        request = request.model_copy(
            update={"current": request.current.model_copy(update={"revision": 2})}
        )
    else:
        async with receiver.factory.begin() as session:
            receipt = await session.get(Receipt, slot)
            if fault == "receipt-state":
                receipt.state = "expired"
            else:
                grant = await session.get(Grant, receipt.grant)
                grant.revision += 1
    before = await durable(receiver)
    with pytest.raises(MaterialDeniedError, match=reason):
        await receiver.renew_preparation(request)
    assert await durable(receiver) == before


async def test_direct_renewal_commits_expiry_seen_after_material_observation(
    hosted, clock, monkeypatch
):
    _, receiver, _ = hosted
    prep, binding, slot = await installed(hosted, clock)
    request = PreparationRenewal.model_validate(renewal(prep, binding, clock))
    original = receiver.status
    initial = clock.instant

    async def advance(value):
        status = await original(value)
        assert status.status == "current"
        clock.instant += timedelta(minutes=11)
        return status

    with monkeypatch.context() as patch:
        patch.setattr(receiver, "status", advance)
        with pytest.raises(MaterialDeniedError, match="GRANT_CONFLICT"):
            await receiver.renew_preparation(request)
    async with receiver.factory() as session:
        assert (await session.get(Receipt, slot)).state == "expired"
        assert len(list(await session.scalars(select(Receipt)))) == 1
    clock.instant = initial
    assert (await receiver.status(Binding.model_validate(binding))).status == "expired"
    assert not list(receiver.store.root.glob("*/*.material"))
