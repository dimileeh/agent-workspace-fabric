"""Import-order regression coverage for metrics modules."""

from __future__ import annotations

import importlib
import sys


def test_import_metrics_slo_before_metrics() -> None:
    module_names = [name for name in list(sys.modules) if name.startswith("awf.service.metrics")]
    for module_name in module_names:
        del sys.modules[module_name]

    importlib.import_module("awf.service.metrics_slo")
    importlib.import_module("awf.service.metrics")
