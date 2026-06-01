"""Lightweight shared helpers for unit tests."""

from __future__ import annotations

import sys

import pytest


def clear_cached_module(monkeypatch: pytest.MonkeyPatch, module_name: str) -> None:
    """Remove a cached module and its parent attribute, if present."""
    parent_name, _, attribute = module_name.rpartition(".")
    parent = sys.modules.get(parent_name)
    if parent is not None:
        monkeypatch.delattr(parent, attribute, raising=False)
    monkeypatch.delitem(sys.modules, module_name, raising=False)
