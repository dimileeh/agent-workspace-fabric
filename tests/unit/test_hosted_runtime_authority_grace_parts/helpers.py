"""Shared helpers for hosted authority grace tests."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Self
from unittest.mock import AsyncMock
from uuid import uuid4

from awf.hosted_node.models import Receipt
from awf.hosted_node.runtime_authority import AuthorizedMaterials
from awf.hosted_node.runtime_consumers import WorkspaceConsumers
from awf.hosted_node.schemas import Binding, Install


async def _install_replacement_grants(runtime, value) -> None:
    """Install higher-fence grants so the association's grants are preparation-superseded."""
    for role, binding in value["hosted"]["credentials"].items():
        replacement = {
            **binding,
            "grant_id": f"{role}-replacement",
            "idempotency_key": f"{role}-replacement",
            "fence": binding["fence"] + 1,
            "material": base64.b64encode(f"replacement-{role}".encode()).decode(),
        }
        result = await runtime.receiver.install(Install.model_validate(replacement))
        assert result.status == "current"


async def _advance_same_grant_current(runtime, factory, selected) -> None:
    """Point grant.current at a newer same-grant receipt (post-selection renewal race)."""
    async with factory.begin() as session:
        for name, prior in selected.items():
            prep = await runtime.receiver.preparation(session, prior)
            assert prep is not None
            grant = await runtime.receiver.grant(session, prep, prior)
            old = await runtime.receiver.receipt(session, prior)
            assert grant is not None and old is not None and grant.current == old.id
            slot = uuid4().hex
            renewed = {
                **runtime.receiver.binding(prior),
                "revision": prior.revision + 1,
                "fence": prior.fence + 1,
                "idempotency_key": f"{name}-post-expiry-renewal",
            }
            session.add(
                Receipt(
                    id=slot,
                    owner=runtime.receiver.identity.owner,
                    idempotency_key=renewed["idempotency_key"],
                    grant=grant.id,
                    binding=renewed,
                    operation="refresh",
                    predecessor=old.id,
                    state="installed",
                )
            )
            grant.current = slot
            grant.revision = prior.revision + 1
            grant.fence = prior.fence + 1


def _advance_past_expiry(runtime, value, monkeypatch, *, extra_seconds: float = 1) -> datetime:
    expiry = Binding.model_validate(value["hosted"]["credentials"]["agent"]).expires_at
    now = expiry + timedelta(seconds=extra_seconds)

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(now.timestamp(), tz=tz)

    monkeypatch.setattr("awf.hosted_node.receiver.datetime", Clock)
    monkeypatch.setattr("awf.hosted_node.runtime_authority.datetime", Clock)
    monkeypatch.setattr("awf.hosted_node.runtime_consumers.datetime", Clock)
    return expiry


def _mock_consumers(tmp_path, *, grace: int = 600) -> tuple[WorkspaceConsumers, AsyncMock]:
    authority = AsyncMock()
    authority.receiver.config.hosted_authority_grace_seconds = grace
    material = AuthorizedMaterials(
        {"GH_TOKEN": "g", "GITHUB_TOKEN": "g", "CODEX_API_KEY": "a"},
        datetime.now(UTC) + timedelta(minutes=1),
    )
    authority.workspace.return_value = material
    scope = WorkspaceConsumers(authority, tmp_path)
    scope.authority_grace_seconds = grace
    scope.poll_seconds = 0.01
    scope.stop_workspace = AsyncMock()
    return scope, authority
