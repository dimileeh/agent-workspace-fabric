"""PricingMetadata model and validation tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError


class TestPricingMetadataModel:
    @pytest.mark.unit
    def test_parses_valid_pricing_metadata(self) -> None:
        from awf.profiles.pricing import PricingMetadata

        meta = PricingMetadata(
            provider="openai",
            model="gpt-5.5",
            currency="USD",
            unit="per_1k_tokens",
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert meta.provider == "openai"
        assert meta.model == "gpt-5.5"
        assert meta.currency == "USD"
        assert meta.unit == "per_1k_tokens"
        assert meta.timestamp == datetime(2026, 1, 1, tzinfo=UTC)
        assert meta.version is None

    @pytest.mark.unit
    def test_rejects_missing_required_field(self) -> None:
        from awf.profiles.pricing import PricingMetadata

        with pytest.raises(ValidationError) as exc_info:
            PricingMetadata(
                provider="openai",
                model="gpt-5.5",
                unit="per_1k_tokens",
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            )
        errors = exc_info.value.errors()
        field_names = {e["loc"][0] for e in errors}
        assert "currency" in field_names

    @pytest.mark.unit
    def test_rejects_empty_currency(self) -> None:
        from awf.profiles.pricing import PricingMetadata

        with pytest.raises(ValidationError) as exc_info:
            PricingMetadata(
                provider="openai",
                model="gpt-5.5",
                currency="",
                unit="per_1k_tokens",
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            )
        errors = exc_info.value.errors()
        field_names = {e["loc"][0] for e in errors}
        assert "currency" in field_names

    @pytest.mark.unit
    def test_rejects_empty_provider(self) -> None:
        from awf.profiles.pricing import PricingMetadata

        with pytest.raises(ValidationError) as exc_info:
            PricingMetadata(
                provider="",
                model="gpt-5.5",
                currency="USD",
                unit="per_1k_tokens",
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            )
        errors = exc_info.value.errors()
        field_names = {e["loc"][0] for e in errors}
        assert "provider" in field_names

    @pytest.mark.unit
    def test_rejects_empty_model(self) -> None:
        from awf.profiles.pricing import PricingMetadata

        with pytest.raises(ValidationError) as exc_info:
            PricingMetadata(
                provider="openai",
                model="",
                currency="USD",
                unit="per_1k_tokens",
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            )
        errors = exc_info.value.errors()
        field_names = {e["loc"][0] for e in errors}
        assert "model" in field_names

    @pytest.mark.unit
    def test_rejects_empty_unit(self) -> None:
        from awf.profiles.pricing import PricingMetadata

        with pytest.raises(ValidationError) as exc_info:
            PricingMetadata(
                provider="openai",
                model="gpt-5.5",
                currency="USD",
                unit="",
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            )
        errors = exc_info.value.errors()
        field_names = {e["loc"][0] for e in errors}
        assert "unit" in field_names

    @pytest.mark.unit
    def test_rejects_naive_timestamp(self) -> None:
        from awf.profiles.pricing import PricingMetadata

        with pytest.raises(ValidationError) as exc_info:
            PricingMetadata(
                provider="openai",
                model="gpt-5.5",
                currency="USD",
                unit="per_1k_tokens",
                timestamp=datetime(2026, 1, 1),
            )
        errors = exc_info.value.errors()
        field_names = {e["loc"][0] for e in errors}
        assert "timestamp" in field_names

    @pytest.mark.unit
    def test_is_current_within_freshness_threshold(self) -> None:
        from awf.profiles.pricing import PricingMetadata

        ts = datetime.now(UTC)
        meta = PricingMetadata(
            provider="openai",
            model="gpt-5.5",
            currency="USD",
            unit="per_1k_tokens",
            timestamp=ts,
        )
        assert meta.is_current(now=ts + timedelta(days=30)) is True

    @pytest.mark.unit
    def test_is_current_false_when_stale(self) -> None:
        from awf.profiles.pricing import PricingMetadata

        ts = datetime.now(UTC)
        meta = PricingMetadata(
            provider="openai",
            model="gpt-5.5",
            currency="USD",
            unit="per_1k_tokens",
            timestamp=ts,
        )
        assert meta.is_current(now=ts + timedelta(days=91)) is False

    @pytest.mark.unit
    def test_is_current_respects_custom_max_age(self) -> None:
        from awf.profiles.pricing import PricingMetadata

        ts = datetime.now(UTC)
        meta = PricingMetadata(
            provider="openai",
            model="gpt-5.5",
            currency="USD",
            unit="per_1k_tokens",
            timestamp=ts,
        )
        assert meta.is_current(now=ts + timedelta(days=60), max_age_days=90) is True
        assert meta.is_current(now=ts + timedelta(days=45), max_age_days=30) is False

    @pytest.mark.unit
    def test_is_current_default_now_is_utc_now(self) -> None:
        from awf.profiles.pricing import PricingMetadata

        meta = PricingMetadata(
            provider="openai",
            model="gpt-5.5",
            currency="USD",
            unit="per_1k_tokens",
            timestamp=datetime.now(UTC),
        )
        assert meta.is_current() is True

    @pytest.mark.unit
    def test_price_per_unit_defaults_to_none(self) -> None:
        from awf.profiles.pricing import PricingMetadata

        meta = PricingMetadata(
            provider="openai",
            model="gpt-5.5",
            currency="USD",
            unit="per_1k_tokens",
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert meta.price_per_unit is None

    @pytest.mark.unit
    def test_accepts_valid_price_per_unit(self) -> None:
        from awf.profiles.pricing import PricingMetadata

        meta = PricingMetadata(
            provider="openai",
            model="gpt-5.5",
            currency="USD",
            unit="per_1k_tokens",
            price_per_unit=0.015,
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert meta.price_per_unit == 0.015

    @pytest.mark.unit
    def test_is_current_false_for_future_timestamp(self) -> None:
        from awf.profiles.pricing import PricingMetadata

        now = datetime.now(UTC)
        future_ts = now + timedelta(days=30)
        meta = PricingMetadata(
            provider="openai",
            model="gpt-5.5",
            currency="USD",
            unit="per_1k_tokens",
            timestamp=future_ts,
        )
        assert meta.is_current(now=now) is False

    @pytest.mark.unit
    def test_is_current_false_for_far_future_timestamp(self) -> None:
        from awf.profiles.pricing import PricingMetadata

        now = datetime.now(UTC)
        meta = PricingMetadata(
            provider="openai",
            model="gpt-5.5",
            currency="USD",
            unit="per_1k_tokens",
            timestamp=now + timedelta(days=365),
        )
        assert meta.is_current(now=now) is False

    @pytest.mark.unit
    def test_is_current_true_at_exact_max_age_boundary(self) -> None:
        from awf.profiles.pricing import PricingMetadata

        ts = datetime.now(UTC)
        meta = PricingMetadata(
            provider="openai",
            model="gpt-5.5",
            currency="USD",
            unit="per_1k_tokens",
            timestamp=ts,
        )
        assert meta.is_current(now=ts + timedelta(days=90)) is True

    @pytest.mark.unit
    def test_is_current_false_just_past_max_age_boundary(self) -> None:
        from awf.profiles.pricing import PricingMetadata

        ts = datetime.now(UTC)
        meta = PricingMetadata(
            provider="openai",
            model="gpt-5.5",
            currency="USD",
            unit="per_1k_tokens",
            timestamp=ts,
        )
        assert meta.is_current(now=ts + timedelta(days=90, seconds=1)) is False

    @pytest.mark.unit
    def test_is_current_true_for_very_recent_past_timestamp(self) -> None:
        from awf.profiles.pricing import PricingMetadata

        now = datetime.now(UTC)
        meta = PricingMetadata(
            provider="openai",
            model="gpt-5.5",
            currency="USD",
            unit="per_1k_tokens",
            timestamp=now - timedelta(minutes=5),
        )
        assert meta.is_current(now=now) is True

    @pytest.mark.unit
    def test_rejects_negative_price_per_unit(self) -> None:
        from awf.profiles.pricing import PricingMetadata

        with pytest.raises(ValidationError) as exc_info:
            PricingMetadata(
                provider="openai",
                model="gpt-5.5",
                currency="USD",
                unit="per_1k_tokens",
                price_per_unit=-0.001,
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            )
        errors = exc_info.value.errors()
        field_names = {e["loc"][0] for e in errors}
        assert "price_per_unit" in field_names


class TestPricingModuleConstants:
    @pytest.mark.unit
    def test_max_age_constant_exists(self) -> None:
        from awf.profiles.pricing import PRICING_MAX_AGE_DAYS

        assert PRICING_MAX_AGE_DAYS == 90
