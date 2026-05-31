"""Regression tests for importing split GitHub client helper modules."""

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


def _clear_split_github_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop cached imports that can mask circular import failures."""
    for module_name in [
        "awf.common.github_client",
        "awf.common.github_client_adoption",
        "awf.common.github_client_parsing",
    ]:
        _clear_cached_module(monkeypatch, module_name)


@pytest.mark.unit
@pytest.mark.parametrize(
    "module_name", ["awf.common.github_client_adoption", "awf.common.github_client_parsing"]
)
def test_github_client_split_modules_import_without_helper_cycle(
    module_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Import each helper module in a clean module state without import errors."""
    _clear_split_github_modules(monkeypatch)
    importlib.import_module(module_name)
