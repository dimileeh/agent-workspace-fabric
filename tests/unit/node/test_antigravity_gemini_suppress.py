"""Antigravity must not stage host ~/.gemini (credentials.enc poison)."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.node.auth_mounts import resolve_service_auth_mounts
from awf.node.stack_launcher import _ANTIGRAVITY_SUPPRESSED_AUTH_MOUNT_TARGETS


@pytest.mark.unit
def test_antigravity_suppressed_auth_mount_targets_include_gemini_home() -> None:
    assert frozenset({"/home/agent/.gemini"}) == _ANTIGRAVITY_SUPPRESSED_AUTH_MOUNT_TARGETS


@pytest.mark.unit
def test_antigravity_suppression_skips_host_gemini_staging(tmp_path: Path) -> None:
    """When antigravity suppression is applied, host ~/.gemini is not staged."""
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    (host_home / ".gemini").mkdir(parents=True)
    (host_home / ".gemini" / "antigravity-cli").mkdir()
    (host_home / ".gemini" / "antigravity-cli" / "credentials.enc").write_text("machine-bound")
    (host_home / ".codex").mkdir()
    (host_home / ".codex" / "auth.json").write_text('{"ok": true}')

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_antigravity",
        host_env={},
        suppressed_targets=_ANTIGRAVITY_SUPPRESSED_AUTH_MOUNT_TARGETS,
    )

    targets = {mount.target for mount in mounts}
    assert "/home/agent/.gemini" not in targets
    assert not (work_dir / "auth" / "ws_antigravity" / "gemini").exists()


@pytest.mark.unit
def test_gemini_runtime_still_stages_host_gemini_without_suppression(tmp_path: Path) -> None:
    """Gemini workspaces continue to stage ~/.gemini when not suppressed."""
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    (host_home / ".gemini").mkdir(parents=True)
    (host_home / ".gemini" / "settings.json").write_text("{}")

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_gemini",
        host_env={},
    )

    assert "/home/agent/.gemini" in {mount.target for mount in mounts}
