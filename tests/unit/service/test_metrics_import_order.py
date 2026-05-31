"""Import-order regression coverage for metrics modules."""

from __future__ import annotations

import importlib
import sys

import pytest

from tests.unit._helpers import clear_cached_module


def test_import_metrics_slo_before_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """Import metrics modules in reverse order without circular import failures."""
    module_names = [name for name in list(sys.modules) if name.startswith("awf.service.metrics")]
    for module_name in module_names:
        clear_cached_module(monkeypatch, module_name)

    importlib.import_module("awf.service.metrics_slo")
    importlib.import_module("awf.service.metrics")
