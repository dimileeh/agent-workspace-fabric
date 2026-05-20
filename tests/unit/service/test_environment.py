"""Unit tests for shared local service environment helpers."""

from __future__ import annotations

from collections.abc import Iterator, Mapping

import pytest


class _ExactKeyOnlyMapping(Mapping[str, str]):
    """Mapping that exposes exact-key lookup but rejects linear scans."""

    def __getitem__(self, key: str) -> str:
        if key == "AWF_TOKEN":
            return "direct-token"
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("env_lookup should use the exact-key fast path")

    def __len__(self) -> int:
        return 1


@pytest.mark.unit
def test_env_lookup_uses_exact_key_fast_path() -> None:
    from awf.service.environment import env_lookup

    assert env_lookup(_ExactKeyOnlyMapping(), "AWF_TOKEN") == (True, "direct-token")


@pytest.mark.unit
def test_env_lookup_fallback_uses_stable_case_variant_priority() -> None:
    from awf.service.environment import env_lookup

    assert env_lookup(
        {"docker_host": "lowercase", "Docker_Host": "mixedcase"},
        "DOCKER_HOST",
    ) == (True, "mixedcase")
    assert env_lookup(
        {"Docker_Host": "mixedcase", "docker_host": "lowercase"},
        "DOCKER_HOST",
    ) == (True, "mixedcase")
