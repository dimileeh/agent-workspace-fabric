"""Import-order regression coverage for provider recovery modules."""

from __future__ import annotations

import importlib
import sys

import pytest

from tests.unit._helpers import clear_cached_module


def _clear_provider_recovery_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    module_names = [
        name
        for name in list(sys.modules)
        if name == "awf.service.provider_recovery"
        or name.startswith("awf.service.provider_recovery_")
    ]
    for module_name in module_names:
        clear_cached_module(monkeypatch, module_name)


def test_import_provider_recovery_state_before_provider_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing provider_recovery_state first must not hit a circular AttributeError."""
    _clear_provider_recovery_modules(monkeypatch)

    state_module = importlib.import_module("awf.service.provider_recovery_state")
    recovery_module = importlib.import_module("awf.service.provider_recovery")

    assert state_module.ProviderRecoveryStateView is recovery_module.ProviderRecoveryStateView
    assert (
        state_module.provider_recovery_state_for_workspace
        is recovery_module.provider_recovery_state_for_workspace
    )


def test_provider_recovery_compat_reexports_remain_importable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compatibility aliases on provider_recovery stay reachable after a fresh import."""
    _clear_provider_recovery_modules(monkeypatch)

    recovery_module = importlib.import_module("awf.service.provider_recovery")
    from awf.service.provider_recovery import (  # noqa: PLC0415
        ProviderRecoveryStateView,
        provider_recovery_decision_from_workspace,
        provider_recovery_state_for_workspace,
    )

    assert ProviderRecoveryStateView is recovery_module.ProviderRecoveryStateView
    assert (
        provider_recovery_state_for_workspace
        is recovery_module.provider_recovery_state_for_workspace
    )
    assert (
        provider_recovery_decision_from_workspace
        is recovery_module.provider_recovery_decision_from_workspace
    )
