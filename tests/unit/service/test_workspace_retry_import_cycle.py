"""Regression test for workspace retry/service import ordering."""

from __future__ import annotations

import importlib

import pytest

from tests.unit._helpers import clear_cached_module


def _clear_cached_workspace_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop selected workspace modules to exercise import ordering from a clean cache."""
    for module_name in [
        "awf.service.workspaces",
        "awf.service.workspaces_create",
        "awf.service.workspaces_response",
        "awf.service.workspaces_retry",
    ]:
        clear_cached_module(monkeypatch, module_name)


@pytest.mark.unit
def test_workspace_retry_imports_without_module_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing `workspaces_retry` from a clean module state should not fail."""
    _clear_cached_workspace_modules(monkeypatch)
    retry_module = importlib.import_module("awf.service.workspaces_retry")
    assert hasattr(retry_module, "retry_workspace_row")


@pytest.mark.unit
def test_workspace_create_imports_without_module_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing `workspaces_create` from a clean module state should not fail."""
    _clear_cached_workspace_modules(monkeypatch)
    create_module = importlib.import_module("awf.service.workspaces_create")
    assert hasattr(create_module, "create_workspace_row")


@pytest.mark.unit
def test_workspace_response_imports_without_module_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing `workspaces_response` before `workspaces` should not fail."""
    _clear_cached_workspace_modules(monkeypatch)
    response_module = importlib.import_module("awf.service.workspaces_response")
    assert hasattr(response_module, "workspace_response")
    workspace_module = importlib.import_module("awf.service.workspaces")
    assert hasattr(workspace_module, "workspace_response")
