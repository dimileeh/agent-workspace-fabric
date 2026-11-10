"""Provider discovery and fixed-fixture diagnostic public contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt

from awf.db.enums import AgentRuntime


class ProviderSpendResponse(BaseModel):
    """Exact provider spend of a managed OpenRouter workspace (USD).

    Summed from the usage counters of the workspace's AWF-minted keys
    (``usage + byok_usage``); ``unavailable`` when the spend was not loaded.
    """

    kind: Literal["exact_provider_spend"] = "exact_provider_spend"
    provider: str = "openrouter"
    status: Literal["available", "unavailable"]
    spend_usd: float | None = None
    budget_usd: float | None = None
    remaining_usd: float | None = None
    currency: Literal["USD"] = "USD"
    key_count: int = 0
    live_key_count: int = 0
    last_read_at: datetime | None = None
    source: Literal["openrouter_key_usage"] = "openrouter_key_usage"
    reason: str | None = None


ProviderBudgetUsd = Annotated[
    StrictFloat | StrictInt | None,
    Field(
        default=None,
        gt=0,
        lt=1_000_000,
        description=(
            "USD budget for managed OpenRouter keys; defaults to "
            "AWF_OPENROUTER_WORKSPACE_LIMIT_USD. Requires a managed OpenRouter selection."
        ),
    ),
]


class ProviderQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: Literal[AgentRuntime.codex, AgentRuntime.claude_code]
    provider: Literal["openai", "anthropic", "ollama", "openrouter"]
    profile_ref: str | None = None
    profile: dict[str, Any] | None = None


class ProviderConnectionTestRequest(ProviderQueryRequest):
    model: str = Field(min_length=1, max_length=500)
    effort: str | None = None
    deadline_seconds: int = Field(default=180, ge=1, le=900)


class ProviderModel(BaseModel):
    id: str
    version: str | None = None
    context_length: int | None = None
    tools: bool | None = None
    pricing: dict[str, str] | None = None


class ProviderCatalogResponse(BaseModel):
    status: Literal["available", "unavailable", "unsupported"]
    reason_code: str
    models: list[ProviderModel] = Field(default_factory=list)
    evidence: Literal["provider_claim"] = "provider_claim"
    coding_compatible: Literal[False] = False
    manual_entry_allowed: Literal[True] = True
    cached: bool = False
    metadata: dict[str, str | None] = Field(default_factory=dict)
    http_status: int | None = None
    checked_at: str
    connection_scope: str


class ProviderConnectionTestResponse(BaseModel):
    workspace_id: str
    operation_id: str
    label: Literal["provider_connection_test"] = "provider_connection_test"
    status: str
