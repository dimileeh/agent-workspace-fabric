"""Ordering regression for the planning-failure artifact deposit.

The console's ``TaskArtifactsSection`` keys its artifact refetch on the
workspace ``updated_at`` (passed as ``refreshKey``). ``_mark_failed`` bumps
``updated_at`` when it publishes the terminal FAILED status, but the
filesystem artifact deposit does not touch the workspace row. If the deposit
ran *after* ``_mark_failed``, the console poll could observe the new
``updated_at`` in the window before the deposit, record an empty artifact
list, then never refetch — leaving the Plan/Validation buttons hidden.

``handle_agent_planning_result`` must therefore deposit the plan/conformance
artifacts *before* marking the workspace FAILED, mirroring every agent-phase
failure handler in ``execution_flow``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import awf.control.executor.planning_artifacts as planning_artifacts
from awf.control.executor.planning_artifacts import (
    _deposit_planning_artifacts_best_effort,
    handle_agent_planning_result,
)


@pytest.mark.unit
async def test_planning_failure_deposits_before_mark_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _record_deposit(self: object, **_kwargs: object) -> None:
        calls.append("deposit")

    monkeypatch.setattr(
        planning_artifacts,
        "_deposit_planning_artifacts_best_effort",
        _record_deposit,
    )

    async def _record_mark_failed(**_kwargs: object) -> None:
        calls.append("mark_failed")

    self = SimpleNamespace(_mark_failed=AsyncMock(side_effect=_record_mark_failed))
    ws = SimpleNamespace(branch_name="feature", remote_push_branch="feature")

    handoff, should_return = await handle_agent_planning_result(
        self,
        workspace_id="ws-1",
        ws=ws,
        worktree_path=Path("/tmp/worktree"),
        profile=SimpleNamespace(),
        planning_failure="conformance unsatisfied",
    )

    assert handoff is None
    assert should_return is True
    # Deposit must precede the terminal-status update so artifact availability
    # is ordered ahead of the console's ``updated_at`` polling signal.
    assert calls == ["deposit", "mark_failed"]


def _profile_with_planning(*, plan_path: str, conformance_report_path: str) -> SimpleNamespace:
    return SimpleNamespace(
        planning=SimpleNamespace(
            required=True,
            plan_path=plan_path,
            conformance_report_path=conformance_report_path,
        )
    )


@pytest.mark.unit
def test_deposit_skips_invalid_profile_template_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An invalid plan_path template is itself the planning failure that routed
    # the workspace here (planning_ops returns ``"planning profile is
    # invalid"`` on the same ValueError). Re-rendering it must NOT raise, or the
    # ValueError escapes before ``_mark_failed`` and strands the workspace in
    # ``running`` instead of FAILED.
    deposited: list[object] = []

    def _fail_if_called(**kwargs: object) -> None:
        deposited.append(kwargs)

    monkeypatch.setattr(
        planning_artifacts,
        "deposit_workspace_planning_artifacts",
        _fail_if_called,
    )

    self = SimpleNamespace(
        _config=SimpleNamespace(compose_projects_root=Path("/tmp/projects/root"))
    )

    # ``..`` makes ``render_workspace_path`` raise ValueError.
    _deposit_planning_artifacts_best_effort(
        self,
        profile=_profile_with_planning(
            plan_path="../escape/plan.md",
            conformance_report_path="conformance.json",
        ),
        workspace_id="ws-1",
        worktree_path=Path("/tmp/worktree"),
    )

    # Render failed → skip the copy entirely rather than depositing or raising.
    assert deposited == []


@pytest.mark.unit
def test_deposit_skips_missing_artifact_root_config_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deposited: list[object] = []

    def _fail_if_called(**kwargs: object) -> None:
        deposited.append(kwargs)

    monkeypatch.setattr(
        planning_artifacts,
        "deposit_workspace_planning_artifacts",
        _fail_if_called,
    )

    self = SimpleNamespace(
        _config=SimpleNamespace(
            max_validation_fix_passes=0,
            planning_max_iterations_default=6,
        )
    )

    _deposit_planning_artifacts_best_effort(
        self,
        profile=_profile_with_planning(
            plan_path="plans/ws-1.md",
            conformance_report_path="reports/ws-1.json",
        ),
        workspace_id="ws-1",
        worktree_path=Path("/tmp/worktree"),
    )

    assert deposited == []


@pytest.mark.unit
async def test_planning_failure_marks_failed_even_with_invalid_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The end-to-end guarantee: an invalid planning profile string failure still
    # reaches ``_mark_failed`` rather than escaping through the deposit helper.
    monkeypatch.setattr(
        planning_artifacts,
        "deposit_workspace_planning_artifacts",
        lambda **_kwargs: None,
    )

    self = SimpleNamespace(
        _config=SimpleNamespace(compose_projects_root=Path("/tmp/projects/root")),
        _mark_failed=AsyncMock(),
    )
    ws = SimpleNamespace(branch_name="feature", remote_push_branch="feature")

    handoff, should_return = await handle_agent_planning_result(
        self,
        workspace_id="ws-1",
        ws=ws,
        worktree_path=Path("/tmp/worktree"),
        profile=_profile_with_planning(
            plan_path="/absolute/plan.md",
            conformance_report_path="conformance.json",
        ),
        planning_failure="planning profile is invalid: path template must stay inside the workspace",
    )

    assert handoff is None
    assert should_return is True
    self._mark_failed.assert_awaited_once()
