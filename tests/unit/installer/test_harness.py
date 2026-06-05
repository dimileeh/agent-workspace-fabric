"""Regression coverage for installer test harness behavior."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.unit.installer.conftest import InstallerHarness


@pytest.mark.unit
def test_awf_stub_escapes_single_quote_in_version(harness: InstallerHarness) -> None:
    """A quoted version token still produces a valid ``awf --version`` stub."""
    awf = harness.add_awf(version="0.1.0'foo")

    result = subprocess.run(
        [awf, "--version"],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "awf 0.1.0'foo\n"


@pytest.mark.unit
def test_shadowed_path_tolerates_broken_symlink_in_two_managed_dirs(
    harness: InstallerHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-named broken symlink across two managed-tool dirs shadows once.

    ``shadowed_system_path`` symlinks each managed-tool directory's non-managed
    executables into a single shadow dir. When such an executable is itself a
    *broken* symlink that recurs in a later managed-tool directory, a
    target-following existence check reports the already-created shadow link as
    absent and re-attempts the symlink, raising ``FileExistsError``. The link
    must be detected by its own presence, first-on-PATH winning.
    """
    first = tmp_path / "first"
    second = tmp_path / "second"
    for directory in (first, second):
        directory.mkdir()
        (directory / "uv").write_text("#!/bin/sh\n", encoding="utf-8")
        # ``dup`` is a dangling symlink in both managed-tool directories.
        (directory / "dup").symlink_to(directory / "missing-target")

    monkeypatch.setenv("PATH", os.pathsep.join([str(first), str(second)]))

    system_path = harness.shadowed_system_path()

    shadow_dir = tmp_path / "syspath-shadow"
    assert str(shadow_dir) in system_path
    # The managed tool disappears; the broken symlink is shadowed exactly once
    # and resolves to the first PATH directory's copy.
    assert not (shadow_dir / "uv").exists(follow_symlinks=False)
    dup_link = shadow_dir / "dup"
    assert dup_link.is_symlink()
    assert dup_link.readlink() == first / "dup"
