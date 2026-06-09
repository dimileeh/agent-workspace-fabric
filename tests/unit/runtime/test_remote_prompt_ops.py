"""Focused tests for PR-monitor protected-scope prompt helpers."""

from __future__ import annotations

import pytest

from awf.runtime.pr_monitor_runner import remote_prompt_ops


class _PromptRunner:
    async def _protected_scope_prompt_sections(
        self,
        *,
        workspace_id: str,
        violations: list[object],
    ) -> tuple[str, str]:
        assert workspace_id == "ws_prompt"
        assert violations == []
        return "  - .github/workflows/ci.yml :: protected :: line 10 :: blocked", "  - src/"


@pytest.mark.unit
async def test_protected_scope_committed_repair_prompt_uses_shared_sections() -> None:
    prompt = await remote_prompt_ops._protected_scope_committed_repair_prompt(  # noqa: SLF001
        _PromptRunner(),
        workspace_id="ws_prompt",
        violations=[],
    )

    assert "already committed locally" in prompt
    assert ".github/workflows/ci.yml" in prompt
    assert "Declared owned_paths" in prompt
