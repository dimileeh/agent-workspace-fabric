"""Executor work-slot binding and call-site injection (slot-release, plan §6.2 C).

The worker binds its per-task work slot with ``executor.work_slot_binding``; every
executor call site that runs an agent, a validation or the PR monitor injects
the bound slot through ``work_slot_kwargs``. Outside a binding no keyword is
passed, so executor fakes and direct callers keep their old signatures.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from awf.control.executor import WorkspaceExecutor
from awf.control.executor.quality_gates import _run_baseline_coverage_preflight
from awf.control.executor.work_slot_binding import work_slot_binding, work_slot_for
from awf.profiles.models import WorkspaceProfile

pytestmark = pytest.mark.unit

_EXECUTOR_DIR = Path(__file__).resolve().parents[3] / "src" / "awf" / "control" / "executor"
_HOOKED_METHODS = {"run_profile_phases", "run_profile_coverage"}
_HOOKED_RECEIVERS_FOR_RUN = {"adapter", "monitor"}


class _Slot:
    async def release_idle(self) -> None:  # pragma: no cover - not exercised here
        return None

    def hold(self, phase: str) -> Any:  # pragma: no cover - not exercised here
        raise AssertionError(phase)


class _RecordingCoverageRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run_profile_coverage(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def _executor() -> WorkspaceExecutor:
    return WorkspaceExecutor(
        session_factory=None,  # type: ignore[arg-type]
        runner=None,  # type: ignore[arg-type]
        compose=None,  # type: ignore[arg-type]
        validation=None,  # type: ignore[arg-type]
        pr_creator=None,  # type: ignore[arg-type]
        config=None,  # type: ignore[arg-type]
    )


def test_binding_is_scoped_to_the_block_and_restores_the_previous_slot() -> None:
    executor = _executor()
    outer, inner = _Slot(), _Slot()

    assert executor._work_slot_for("ws_1") is None
    with executor.work_slot_binding("ws_1", outer):
        assert work_slot_for(executor, "ws_1") is outer
        assert work_slot_for(executor, "ws_2") is None
        with executor.work_slot_binding("ws_1", inner):
            assert work_slot_for(executor, "ws_1") is inner
        assert work_slot_for(executor, "ws_1") is outer
        # Binding ``None`` leaves the current slot in place.
        with executor.work_slot_binding("ws_1", None):
            assert work_slot_for(executor, "ws_1") is outer
    assert work_slot_for(executor, "ws_1") is None


def test_lookup_tolerates_executor_fakes_and_missing_workspace_ids() -> None:
    assert work_slot_for(SimpleNamespace(), "ws_1") is None
    assert work_slot_for(SimpleNamespace(_work_slots=None), "ws_1") is None
    fake = SimpleNamespace(_work_slots={})
    with work_slot_binding(fake, "ws_1", _Slot()):
        assert work_slot_for(fake, None) is None


async def test_bound_slot_reaches_validation_and_unbound_passes_nothing(
    tmp_path: Path,
) -> None:
    executor = _executor()
    runner = _RecordingCoverageRunner()
    profile = WorkspaceProfile.model_validate(
        {
            "name": "slot",
            "validation": {"coverage": {"command": "coverage report", "minimum_percent": 1}},
        }
    )
    kwargs: dict[str, Any] = {
        "workspace_id": "ws_1",
        "compose_project": "proj",
        "compose_file": tmp_path / "compose.yml",
        "profile": profile,
        "coverage_runner": runner,
    }
    slot = _Slot()

    await _run_baseline_coverage_preflight(executor, **kwargs)
    with executor.work_slot_binding("ws_1", slot):
        await _run_baseline_coverage_preflight(executor, **kwargs)

    assert "work_slot" not in runner.calls[0]
    assert runner.calls[1]["work_slot"] is slot


def _call_is_hooked(node: ast.Call) -> bool:
    if not isinstance(node.func, ast.Attribute):
        return False
    attr = node.func.attr
    receiver = node.func.value
    receiver_name = (
        receiver.id
        if isinstance(receiver, ast.Name)
        else receiver.attr
        if isinstance(receiver, ast.Attribute)
        else None
    )
    if attr in _HOOKED_METHODS:
        return True
    return attr == "run" and receiver_name in _HOOKED_RECEIVERS_FOR_RUN


def _passes_work_slot(node: ast.Call) -> bool:
    for keyword in node.keywords:
        if keyword.arg == "work_slot":
            return True
        if keyword.arg is None and "work_slot_kwargs" in ast.unparse(keyword.value):
            return True
    return False


def test_every_executor_agent_validation_and_monitor_call_injects_the_bound_slot() -> None:
    # Regression guard: a new agent/validation/monitor call site that forgets the
    # injection would run outside the worker's work slot (or queue behind it).
    missing: list[str] = []
    seen = 0
    for path in sorted(_EXECUTOR_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_is_hooked(node):
                seen += 1
                if not _passes_work_slot(node):
                    missing.append(f"{path.name}:{node.lineno}")
    assert seen >= 19
    assert missing == []
