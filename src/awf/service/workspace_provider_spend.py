"""Exact provider spend for managed OpenRouter workspaces (show/list/overview/tasks).

Spend comes from AWF-minted keys' usage counters (``provider_managed_keys``),
summed per workspace in one grouped query. Response projections are
synchronous, so async callers load the spend first and scope it with
``provider_spend_scope`` (the ``prefetched_usage_snapshots`` idiom). A managed
workspace projected outside a scope reports ``provider_spend_not_loaded``
instead of inventing a cost.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from decimal import Decimal
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.agent_execution import managed_openrouter_frozen
from awf.db.repositories import ProviderManagedKeyRepository, ProviderSpendSummary
from awf.service.provider_managed_keys import budget_from_policy

EXACT_PROVIDER_SPEND_SOURCE: Final[str] = "openrouter_key_usage"
_PREFETCHED_PROVIDER_SPEND: ContextVar[Mapping[str, ProviderSpendSummary | None] | None] = (
    ContextVar("awf_prefetched_provider_spend", default=None)
)


@contextmanager
def prefetched_provider_spend(
    spend: Mapping[str, ProviderSpendSummary | None],
) -> Iterator[None]:
    """Scope pre-loaded spend (``None`` = a loaded workspace without keys)."""
    token = _PREFETCHED_PROVIDER_SPEND.set(spend)
    try:
        yield
    finally:
        _PREFETCHED_PROVIDER_SPEND.reset(token)


@asynccontextmanager
async def provider_spend_scope(
    session: AsyncSession, workspace_ids: Iterable[str]
) -> AsyncIterator[None]:
    """Load the spend of ``workspace_ids`` in one grouped query and scope it."""
    ids = list(dict.fromkeys(workspace_id for workspace_id in workspace_ids if workspace_id))
    summaries = await ProviderManagedKeyRepository(session).spend_by_workspace_ids(ids)
    loaded: dict[str, ProviderSpendSummary | None] = dict.fromkeys(ids)
    loaded.update(summaries)
    with prefetched_provider_spend(loaded):
        yield


def provider_spend_payload(workspace: Any) -> dict[str, Any] | None:
    """The ``provider_spend`` projection; ``None`` for non-managed workspaces."""
    policy = getattr(workspace, "task_policy", None)
    if not managed_openrouter_frozen(policy):
        return None
    budget = budget_from_policy(policy)
    payload: dict[str, Any] = {
        "kind": "exact_provider_spend",
        "provider": "openrouter",
        "status": "unavailable",
        "spend_usd": None,
        "budget_usd": float(budget) if budget is not None else None,
        "remaining_usd": None,
        "currency": "USD",
        "key_count": 0,
        "live_key_count": 0,
        "last_read_at": None,
        "source": EXACT_PROVIDER_SPEND_SOURCE,
        "reason": "provider_spend_not_loaded",
    }
    prefetched = _PREFETCHED_PROVIDER_SPEND.get()
    workspace_id = str(getattr(workspace, "id", None) or "")
    if prefetched is None or workspace_id not in prefetched:
        return payload
    summary = prefetched[workspace_id]
    spend = summary.spend_usd if summary is not None else Decimal(0)
    payload.update(
        status="available",
        spend_usd=float(spend),
        remaining_usd=float(max(Decimal(0), budget - spend)) if budget is not None else None,
        key_count=summary.key_count if summary is not None else 0,
        live_key_count=summary.live_key_count if summary is not None else 0,
        last_read_at=summary.last_read_at if summary is not None else None,
        reason=None,
    )
    return payload


def apply_exact_spend(llm_usage: Any, provider_spend: Mapping[str, Any] | None) -> Any:
    """Report the exact provider spend as the usage cost of a managed run."""
    if provider_spend is None or provider_spend["status"] != "available":
        return llm_usage
    return {
        **llm_usage,
        "cost_estimate": provider_spend["spend_usd"],
        "currency": "USD",
        "status": "available",
        "source": EXACT_PROVIDER_SPEND_SOURCE,
        "reason": None,
    }
