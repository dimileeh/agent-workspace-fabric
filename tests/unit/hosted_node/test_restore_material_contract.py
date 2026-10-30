"""Every final credential field is authoritative, independently of SQL selection."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.restore_reconciliation_apply import verify_current_materials
from awf.hosted_node.schemas import Binding
from tests.unit.test_hosted_runtime_contract import hosted_create


def selected():
    binding = Binding.model_validate(hosted_create()["hosted"]["credentials"]["agent"])
    binding = binding.model_copy(update={"expires_at": datetime.now(UTC) + timedelta(minutes=1)})
    prep = SimpleNamespace(
        owner=binding.identity.owner,
        namespace=binding.namespace,
        workspace_id=binding.workspace_id,
        preparation_id=binding.preparation_id,
        binding={
            **binding.model_dump(mode="json"),
            "expires_at": (binding.expires_at + timedelta(seconds=1)).isoformat(),
        },
    )
    grant = SimpleNamespace(
        current="receipt",
        pending=None,
        revoked=False,
        fence=binding.fence,
        revision=binding.revision,
        authorization_generation=binding.authorization_generation,
        grant_id=binding.grant_id,
    )
    receipt = SimpleNamespace(
        id="receipt",
        idempotency_key=binding.idempotency_key,
        binding=binding.model_dump(mode="json"),
        state="installed",
        owner=binding.identity.owner,
    )
    return binding, prep, grant, receipt


async def test_final_credential_vector_accepts_exact_bindings_and_empty_plan():
    binding, prep, grant, receipt = selected()
    session = AsyncMock()
    await verify_current_materials(session, [])
    session.execute.assert_not_called()
    session.execute.return_value = Mock(
        all=Mock(
            return_value=[(prep, grant, receipt, binding.fence, binding.authorization_generation)]
        )
    )
    await verify_current_materials(session, [binding])
    session.execute.assert_awaited_once()


@pytest.mark.parametrize(
    "fault",
    [
        "binding",
        "state",
        "current",
        "pending",
        "revoked",
        "fence",
        "max_fence",
        "revision",
        "generation",
        "max_generation",
        "prep_owner",
        "receipt_owner",
        "identity",
        "namespace",
        "workspace",
        "preparation",
        "grant",
        "target",
        "purpose",
        "expired",
        "beyond_preparation",
    ],
)
async def test_each_final_credential_field_is_required(fault):
    binding, prep, grant, receipt = selected()
    fence, generation = binding.fence, binding.authorization_generation
    if fault == "binding":
        receipt.binding = {}
    elif fault == "state":
        receipt.state = "stale"
    elif fault in {"current", "pending", "revoked", "fence", "revision"}:
        setattr(
            grant,
            fault,
            {
                "current": "wrong",
                "pending": "pending",
                "revoked": True,
                "fence": 999,
                "revision": 999,
            }[fault],
        )
    elif fault == "max_fence":
        fence += 1
    elif fault == "max_generation":
        generation += 1
    elif fault == "generation":
        grant.authorization_generation += 1
    elif fault == "prep_owner":
        prep.owner = "other/node"
    elif fault == "receipt_owner":
        receipt.owner = "other/node"
    elif fault == "identity":
        prep.binding["identity"] = {}
    elif fault in {"namespace", "workspace", "preparation"}:
        setattr(
            prep,
            {
                "namespace": "namespace",
                "workspace": "workspace_id",
                "preparation": "preparation_id",
            }[fault],
            "different",
        )
    elif fault == "grant":
        grant.grant_id = "different"
    elif fault in {"target", "purpose"}:
        prep.binding[fault] = "different"
    elif fault == "expired":
        binding = binding.model_copy(
            update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
        )
        receipt.binding = binding.model_dump(mode="json")
    else:
        prep.binding["expires_at"] = (binding.expires_at - timedelta(seconds=1)).isoformat()
    session = AsyncMock(
        execute=AsyncMock(
            return_value=Mock(all=Mock(return_value=[(prep, grant, receipt, fence, generation)]))
        )
    )
    with pytest.raises(MaterialDeniedError, match="RESTORE_MATERIALS_REQUIRED"):
        await verify_current_materials(session, [binding])
