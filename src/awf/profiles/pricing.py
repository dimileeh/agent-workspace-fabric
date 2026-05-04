"""Pricing metadata contract for reliable cost estimation.

Defines an explicit pricing metadata model that profiles can optionally carry.
Cost estimates are never fabricated from unknown pricing — the model enforces
required fields (provider, model, currency, unit, timestamp) and freshness.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

PRICING_MAX_AGE_DAYS = 90


class PricingMetadata(BaseModel):
    """Explicit pricing metadata for a model-provider combination.

    A profile may optionally declare pricing so that cost estimates can be
    computed when both token usage data and trusted pricing metadata exist.
    When pricing is absent, stale, or unknown, the API and console clearly
    explain that pricing is unavailable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Annotated[str, Field(min_length=1, max_length=128)]
    model: Annotated[str, Field(min_length=1, max_length=256)]
    currency: Annotated[str, Field(min_length=1, max_length=16)]
    unit: Annotated[str, Field(min_length=1, max_length=64)]
    price_per_unit: float | None = Field(default=None, ge=0)
    timestamp: datetime
    version: int | None = None

    @field_validator("timestamp")
    @classmethod
    def _require_utc_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be UTC-aware")
        return value

    def is_current(
        self,
        now: datetime | None = None,
        max_age_days: int = PRICING_MAX_AGE_DAYS,
    ) -> bool:
        now_utc = (now or datetime.now(UTC)).astimezone(UTC)
        timestamp_utc = self.timestamp.astimezone(UTC)
        age = now_utc - timestamp_utc
        return timedelta(0) <= age <= timedelta(days=max_age_days)
