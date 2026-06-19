"""Error-path coverage for GitManager mirror hook repair."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from awf.node import git_manager as git_module
from awf.node.git_manager import GitOperationError


@pytest.mark.unit
async def test_repair_mirror_hooks_path_raises_on_probe_failure(tmp_path: Path) -> None:
    mirror = tmp_path / "missing.git"

    with pytest.raises(GitOperationError) as exc:
        await git_module.repair_mirror_hooks_path(mirror)

    assert exc.value.operation == "mirror.hooks_path_probe"
    assert exc.value.returncode != 1
    assert exc.value.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
    assert exc.value.stderr


@pytest.mark.unit
async def test_repair_mirror_hooks_path_raises_on_unset_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror = tmp_path / "mirror.git"
    mirror.mkdir()
    subprocess.run(
        ["git", "init", "--bare", str(mirror)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "--git-dir", str(mirror), "config", "core.hooksPath", "/dev/null"],
        check=True,
        capture_output=True,
    )

    original_exec = asyncio.create_subprocess_exec
    call_count = 0

    async def _fake_exec(*args: object, **kwargs: object) -> object:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            return await original_exec(
                "sh",
                "-c",
                "exit 5",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        return await original_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    with pytest.raises(GitOperationError) as exc:
        await git_module.repair_mirror_hooks_path(mirror)

    assert exc.value.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
