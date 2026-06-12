"""Ordering regression for the validation-worktree guard artifact deposit.

The pre-validation worktree guards (dirty worktree, missing HEAD, post-fix-pass
dirty check) and the validation cleanup guard all funnel their terminal FAILED
transition through ``fail_validation_worktree_guard``. The console's
``TaskArtifactsSection`` keys its artifact refetch on the workspace
``updated_at`` (passed as ``refreshKey``); ``_mark_failed`` bumps ``updated_at``
when it publishes FAILED, but the filesystem artifact deposit does not touch the
row. If the deposit ran *after* ``_mark_failed``, a console poll could observe
the new ``updated_at`` in the window before the deposit, record an empty
artifact list, then never refetch — leaving the Plan/Validation buttons hidden.

``fail_validation_worktree_guard`` must therefore deposit the plan/conformance
artifacts *before* marking the workspace FAILED, mirroring every other terminal
path in ``run_validation_and_fix_cycle``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.control.executor import validation_cleanup_guards as guards
from awf.control.executor.validation_cleanup_guards import fail_validation_worktree_guard


@pytest.mark.unit
async def test_worktree_guard_deposits_before_mark_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _record_deposit(self: object, **kwargs: object) -> None:
        # The deposit must carry the resolved profile + worktree so its
        # ``planning.required`` gate and copy source stay correct.
        assert kwargs["profile"] is profile
        assert kwargs["worktree_path"] == worktree_path
        calls.append("deposit")

    monkeypatch.setattr(
        guards,
        "_deposit_planning_artifacts_best_effort",
        _record_deposit,
    )

    async def _record_mark_failed(**_kwargs: object) -> None:
        calls.append("mark_failed")

    self = SimpleNamespace(
        _finish_validation_run=AsyncMock(),
        _finish_pending_validate_operations=AsyncMock(),
        _mark_failed=AsyncMock(side_effect=_record_mark_failed),
    )
    profile = SimpleNamespace()
    worktree_path = Path("/tmp/worktree")

    result = await fail_validation_worktree_guard(
        self,
        workspace_id="ws-1",
        validation_run_id="vr-1",
        validation_tier=1,
        reason_code="VALIDATION_WORKTREE_PRE_EXISTING_DIRTY",
        message="worktree dirty before validation",
        profile=profile,
        worktree_path=worktree_path,
    )

    assert result.stop is True
    # Deposit must precede the terminal-status update so artifact availability
    # is ordered ahead of the console's ``updated_at`` polling signal.
    assert calls == ["deposit", "mark_failed"]
