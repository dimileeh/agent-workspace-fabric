"""Unit tests for PR monitor provider-recovery metadata helpers."""

from __future__ import annotations

import pytest

from awf.runtime.pr_monitor_runner.loop_recovery_ops import (
    _attach_provider_recovery_details,
    _provider_recovery_operation_result_updates,
)
from awf.runtime.pr_monitor_runner.types import ProviderRecoveryRetryError


@pytest.mark.unit
def test_attach_provider_recovery_details_noop_when_details_empty() -> None:
    exc = ProviderRecoveryRetryError()
    _attach_provider_recovery_details(exc, {})
    assert getattr(exc, "details", None) is None


@pytest.mark.unit
def test_attach_provider_recovery_details_merges_existing_mapping() -> None:
    exc = ProviderRecoveryRetryError(details={"phase": "ci_repair_commit_sink"})
    _attach_provider_recovery_details(
        exc,
        {"repair_salvage": {"patch_path": "/tmp/ws.patch"}},
    )
    assert exc.details == {
        "phase": "ci_repair_commit_sink",
        "repair_salvage": {"patch_path": "/tmp/ws.patch"},
    }


@pytest.mark.unit
def test_attach_provider_recovery_details_sets_when_missing() -> None:
    exc = ProviderRecoveryRetryError()
    _attach_provider_recovery_details(exc, {"stranded_paths": ["src/fix.py"]})
    assert exc.details == {"stranded_paths": ["src/fix.py"]}


@pytest.mark.unit
def test_provider_recovery_operation_result_updates_returns_empty_for_non_dict_details() -> None:
    exc = Exception()
    assert _provider_recovery_operation_result_updates(exc) == {}


@pytest.mark.unit
def test_provider_recovery_operation_result_updates_extracts_salvage_fields() -> None:
    exc = ProviderRecoveryRetryError(
        details={
            "repair_salvage": {"patch_path": "/tmp/ws.patch"},
            "stranded_paths": ["src/fix.py"],
            "phase": "ci_repair_commit_sink",
            "provider_error_stderr": "MODEL_CAPACITY_EXHAUSTED",
            "salvage_error": {"reason_code": "REPAIR_SALVAGE_UNEXPECTED"},
            "rollback_error": {"cause": "reset_failed"},
        }
    )
    assert _provider_recovery_operation_result_updates(exc) == {
        "repair_salvage": {"patch_path": "/tmp/ws.patch"},
        "stranded_paths": ["src/fix.py"],
        "phase": "ci_repair_commit_sink",
        "provider_error_stderr": "MODEL_CAPACITY_EXHAUSTED",
        "salvage_error": {"reason_code": "REPAIR_SALVAGE_UNEXPECTED"},
        "rollback_error": {"cause": "reset_failed"},
    }
