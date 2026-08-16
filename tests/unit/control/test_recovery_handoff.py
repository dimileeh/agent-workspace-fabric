"""Focused recovery handoff metadata-update error handling tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.control.executor.recovery_handoff import handle_recovery_pr_handoff_after_validation
from awf.control.executor.types import _RebaseRecoveryResult
from awf.profiles.models import WorkspaceProfile


async def _noop_repair(**_kwargs: object) -> bool:
    return False


async def _unexpected_clear_rebase_recovery_staleness(**_kwargs: object) -> None:
    pytest.fail("staleness clear should not run after target-head update failure")


@pytest.mark.unit
async def test_rebase_recovery_metadata_runtime_error_propagates() -> None:
    async def _raise_target_head_update(**_kwargs: object) -> None:
        raise RuntimeError("target-head update bug")

    context = SimpleNamespace(
        _set_validation_run_target_head_sha=_raise_target_head_update,
        _clear_rebase_recovery_staleness=_unexpected_clear_rebase_recovery_staleness,
    )

    with pytest.raises(RuntimeError, match="target-head update bug"):
        await handle_recovery_pr_handoff_after_validation(
            context,
            workspace_id="ws_recovery",
            ws=SimpleNamespace(pr_url="https://github.com/x/y/pull/1"),
            recovery={"recovery_mode": "rebase_only"},
            rebase_recovery_result=_RebaseRecoveryResult(
                base_sha="a" * 40,
                head_sha="b" * 40,
                requires_pr_update=True,
            ),
            successful_validation_run_id="vr_1",
            successful_validation_workspace_head_sha="b" * 40,
            repair_mirror_hooks_path_or_mark_failed=_noop_repair,
            adapter=SimpleNamespace(),
            profile=WorkspaceProfile(name="test"),
            defaults=None,
            compose_project="awf_ws_recovery",
            compose_file=Path("/tmp/missing-compose.yml"),
        )


@pytest.mark.unit
async def test_validate_only_recovery_metadata_runtime_error_propagates() -> None:
    async def _raise_target_head_update(**_kwargs: object) -> None:
        raise RuntimeError("validate-only target update bug")

    context = SimpleNamespace(_set_validation_run_target_head_sha=_raise_target_head_update)
    head_sha = "c" * 40

    with pytest.raises(RuntimeError, match="validate-only target update bug"):
        await handle_recovery_pr_handoff_after_validation(
            context,
            workspace_id="ws_validate_only",
            ws=SimpleNamespace(pr_url="https://github.com/x/y/pull/1"),
            recovery={"recovery_mode": "validate_only", "source_head_sha": head_sha},
            rebase_recovery_result=None,
            successful_validation_run_id="vr_2",
            successful_validation_workspace_head_sha=head_sha,
            repair_mirror_hooks_path_or_mark_failed=_noop_repair,
            adapter=SimpleNamespace(),
            profile=WorkspaceProfile(name="test"),
            defaults=None,
            compose_project="awf_ws_validate_only",
            compose_file=Path("/tmp/missing-compose.yml"),
        )
