"""The protected-scope block notification names the preserved commit (#1200).

Operators answered a protected-scope pause with "revert the workflow change"; the
agent added a revert on top, which keeps the preserved commit in the push range and
re-blocks. The notification therefore names the preserved commit and says how to
discard it: reset to the published PR head, don't revert.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from awf.control.quality_gates import QualityGateViolation
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import remote_repair_protected

pytestmark = pytest.mark.unit

_PUBLISHED = "8" * 40
_PRESERVED = "3" * 40


class _FakeGitHub:
    def __init__(self) -> None:
        self.bodies: list[str] = []

    async def post_comment(self, **kwargs: object) -> None:
        body = kwargs["body"]
        assert isinstance(body, str)
        self.bodies.append(body)


async def _notify(preserved_head_sha: str | None) -> str:
    github = _FakeGitHub()
    await remote_repair_protected._post_protected_block_notification(
        SimpleNamespace(_deps=SimpleNamespace(gh=github)),
        workspace_id="ws-protected-reset",
        pr_number=42,
        repo_url="https://github.com/example/service",
        pr_head_sha=_PUBLISHED,
        state=MonitorState(),
        violations=(
            QualityGateViolation(
                path=".github/workflows/ci.yml", protected_pattern=".github/workflows/"
            ),
        ),
        block_epoch=1,
        preserved_head_sha=preserved_head_sha,
    )
    assert len(github.bodies) == 1
    return github.bodies[0]


async def test_notification_names_preserved_commit_and_says_reset_not_revert() -> None:
    body = await _notify(_PRESERVED)

    assert f"Preserved commit {_PRESERVED} is not pushed" in body
    assert "reset, don't revert" in body
    assert f"move the branch back to the published PR head {_PUBLISHED}" in body
    assert "a revert-on-top commit keeps it in the push range" in body


async def test_notification_without_preserved_commit_has_no_reset_hint() -> None:
    body = await _notify(None)

    assert "Preserved commit" not in body
    assert "reset, don't revert" not in body
