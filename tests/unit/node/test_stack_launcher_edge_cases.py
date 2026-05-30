"""Edge-case coverage for the workspace stack launcher.

Complements ``test_stack_launcher.py`` (kept in a separate module so the main
suite stays under the first-party file line cap). Covers the
``DOCKER_UNAVAILABLE`` path with no required services / empty diagnostics and
the parsed-spec branch of the companion compose-up timeout helper.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.node.companion_services import WorkspaceCompanionSpec
from awf.node.compose_manager import ComposeOperationError, WorkspaceComposeSpec
from awf.node.git_manager import WorktreeLayout
from awf.node.stack_launcher import (
    ComposeStackLauncher,
    WorkspaceServiceExecutionError,
    WorkspaceStackLaunchRequest,
    effective_compose_up_timeout_seconds,
)
from awf.profiles.models import ProfileDocker, WorkspaceProfile


class _UnavailableCompose:
    def __init__(self) -> None:
        self.specs: list[WorkspaceComposeSpec] = []

    async def up(self, spec: WorkspaceComposeSpec, *, wait: bool = True) -> None:
        del wait
        self.specs.append(spec)
        raise ComposeOperationError(
            operation="up",
            returncode=1,
            stdout="",
            stderr="",
            reason_code="DOCKER_UNAVAILABLE",
        )


def _layout() -> WorktreeLayout:
    return WorktreeLayout(
        mirror_path=Path("/host/awf/git/mirrors/repo.git"),
        worktree_path=Path("/host/awf/git/worktrees/ws_edge"),
        branch_name="awf/ws_edge",
    )


@pytest.mark.unit
async def test_docker_unavailable_without_required_services_or_detail() -> None:
    launcher = ComposeStackLauncher(
        compose=_UnavailableCompose(),  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )

    with pytest.raises(WorkspaceServiceExecutionError) as raised:
        await launcher.launch(
            WorkspaceStackLaunchRequest(
                workspace_id="ws_edge",
                layout=_layout(),
                profile=WorkspaceProfile(name="bare"),
            )
        )

    message = str(raised.value)
    assert message == "DOCKER_UNAVAILABLE: Cannot start workspace agent container"
    assert "required services" not in message


@pytest.mark.unit
def test_effective_timeout_uses_parsed_companion_spec_timeout() -> None:
    profile = WorkspaceProfile(name="serviceful", docker=ProfileDocker(startup_timeout_seconds=300))
    companion = WorkspaceCompanionSpec(
        name="backend",
        repo_url="https://github.com/example/backend.git",
        compose_up_timeout_seconds=900,
    )

    assert effective_compose_up_timeout_seconds(profile=profile, companions=(companion,)) == 900
