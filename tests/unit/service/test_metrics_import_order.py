"""Import-order regression coverage for metrics modules."""

from __future__ import annotations

import importlib
import sys

import pytest


def _clear_cached_module(monkeypatch: pytest.MonkeyPatch, module_name: str) -> None:
    parent_name, _, attribute = module_name.rpartition(".")
    parent = sys.modules.get(parent_name)
    if parent is not None:
        monkeypatch.delattr(parent, attribute, raising=False)
    monkeypatch.delitem(sys.modules, module_name, raising=False)


def test_import_metrics_slo_before_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    module_names = [name for name in list(sys.modules) if name.startswith("awf.service.metrics")]
    for module_name in module_names:
        _clear_cached_module(monkeypatch, module_name)

    importlib.import_module("awf.service.metrics_slo")
    importlib.import_module("awf.service.metrics")
