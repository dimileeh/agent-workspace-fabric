"""Fixtures shared by the worktree activity probe part modules."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.adapters.test_worktree_activity_probe_parts.helpers import _age_tree


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    root = tmp_path / "ws_probe"
    (root / "src" / "nested").mkdir(parents=True)
    (root / "src" / "nested" / "module.py").write_text("x = 1\n", encoding="utf-8")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _age_tree(root)
    return root
