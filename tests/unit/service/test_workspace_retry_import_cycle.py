"""Regression test for workspace retry/service import ordering."""

from __future__ import annotations

import importlib
import sys

import pytest


def _clear_cached_workspace_modules() -> None:
    """Drop selected workspace modules to exercise import ordering from a clean cache."""
    for module_name in [
        "awf.service.workspaces",
        "awf.service.workspaces_create",
        "awf.service.workspaces_response",
        "awf.service.workspaces_retry",
    ]:
        sys.modules.pop(module_name, None)


@pytest.mark.unit
def test_workspace_retry_imports_without_module_cycle() -> None:
    """Importing `workspaces_retry` from a clean module state should not fail."""
    _clear_cached_workspace_modules()
    retry_module = importlib.import_module("awf.service.workspaces_retry")
    assert hasattr(retry_module, "retry_workspace_row")


@pytest.mark.unit
def test_workspace_create_imports_without_module_cycle() -> None:
    """Importing `workspaces_create` from a clean module state should not fail."""
    _clear_cached_workspace_modules()
    create_module = importlib.import_module("awf.service.workspaces_create")
    assert hasattr(create_module, "create_workspace_row")
