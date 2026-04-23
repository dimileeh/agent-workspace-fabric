"""Tests for ``scripts.salvage_workspace`` no-op adapter factory.

Scope: the closure-capture fix (CodeRabbit PR #2 feedback). Classes
defined inside a for-loop that reference the loop variable share it by
reference, so every factory was bound to the SAME runtime (the last
one the loop saw) — a silent bug that would only surface when
salvaging a workspace whose runtime wasn't the registry's final key.

We don't exercise the real adapter path; just verify every factory
reports its OWN runtime after ``_install_noop_adapter_factory`` runs.
"""

from __future__ import annotations

import pytest

from awf.adapters import base as _adapter_base
from awf.adapters import registry as _registry  # noqa: F401 - populates registry
from awf.db.enums import AgentRuntime
from scripts.salvage_workspace import _install_noop_adapter_factory, _make_noop_factory


class TestClosureCapture:
    @pytest.mark.unit
    def test_each_factory_binds_its_own_runtime(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """After installation, each registry entry must return an
        adapter whose ``.name`` matches ITS key. The closure-capture
        bug made every entry report the last-iterated runtime instead."""
        # Snapshot + restore registry so the test doesn't pollute.
        original_registry = dict(_adapter_base._REGISTRY)
        monkeypatch.setattr(_adapter_base, "_REGISTRY", dict(original_registry))

        _install_noop_adapter_factory()

        for registered_runtime, factory_cls in _adapter_base._REGISTRY.items():
            instance = factory_cls(runner=None, default_model=None)
            assert instance.name == registered_runtime, (
                f"factory for {registered_runtime.value} reports "
                f"{instance.name.value} — closure-capture regression"
            )

    @pytest.mark.unit
    def test_factory_builder_isolation(self) -> None:
        """``_make_noop_factory`` is the extraction that fixed the bug —
        each call must produce a class bound to the argument value, not
        to whatever state the caller's loop variable happens to hold
        later. Build two factories back-to-back, mutate nothing, and
        verify they keep their distinct runtimes."""
        codex_factory = _make_noop_factory(AgentRuntime.codex)
        claude_factory = _make_noop_factory(AgentRuntime.claude_code)

        assert codex_factory().name == AgentRuntime.codex
        assert claude_factory().name == AgentRuntime.claude_code
