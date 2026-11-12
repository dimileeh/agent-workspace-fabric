from __future__ import annotations

from types import SimpleNamespace

import pytest

from awf.api.schemas import WorkspaceCreateRequest
from awf.db.enums import AgentRuntime
from awf.service.workspaces_create import (
    workspace_create_payload_matches,
    workspace_create_task_policy_snapshot,
)

pytestmark = pytest.mark.unit


def test_create_replay_ignores_attached_fallback_compatibility_evidence() -> None:
    request = WorkspaceCreateRequest(
        repo={"url": "https://github.com/example/repo.git", "base_branch": "main"},
        task={
            "title": "Verified fallback",
            "prompt": "Use a verified third-party fallback",
            "agent": "codex",
            "provider_recovery": {
                "fallbacks": [{"agent": "opencode", "provider": "acme", "model": "acme-coder"}],
            },
        },
    )
    policy = workspace_create_task_policy_snapshot(request)
    fallback = policy["provider_recovery"]["fallbacks"][0]
    fallback["agent_execution"] = {"provider": "acme", "third_party": True}
    fallback["coding_compatibility_evidence"] = {"fixture_version": "v1"}
    existing = SimpleNamespace(
        repo_url=request.repo.url,
        branch_base=request.repo.base_branch,
        task_tag=None,
        task_title=request.task.title,
        task_prompt=request.task.prompt,
        task_external_id=None,
        task_class=None,
        owned_paths=[],
        task_policy=policy,
        auto_merge=False,
        initial_review_grace_period_seconds=None,
        agent=AgentRuntime.codex.value,
        task_kind="feature_branch_pr",
        profile_ref="auto",
        requested_profile=None,
        resolved_profile=None,
        test_commands=[],
        resource_reservations=[],
        requires_database=False,
        env_profile=None,
    )

    assert workspace_create_payload_matches(existing, request)
    fallback["model"] = "other-model"
    assert not workspace_create_payload_matches(existing, request)
