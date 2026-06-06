"""Marker-gated uv removal for ``packaging/uninstall.sh`` (T21).

``--remove-uv`` removes uv only when an AWF uv-ownership marker (written by
``install.sh``'s ``bootstrap_uv``) proves AWF bootstrapped it; otherwise it
refuses with ``UV_REMOVAL_REFUSED_UNOWNED`` so a user's own uv is never removed.
The removal is destructive, so it needs ``--yes`` or an interactive confirmation,
and ``--dry-run`` only plans it.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.unit.installer.conftest import UNINSTALLER, InstallerHarness

PACKAGE = "agent-workspace-fabric"


def _env(harness: InstallerHarness) -> dict[str, str]:
    """Seams pinning both ``AWF_HOME`` and the marker path to the hermetic tree."""
    return {
        "AWF_HOME": str(harness.awf_home()),
        "AWF_UV_MARKER": str(harness.uv_marker_path()),
    }


def _run(
    harness: InstallerHarness, args: list[str], **kwargs: object
) -> subprocess.CompletedProcess[str]:
    extra_env = dict(_env(harness))
    extra_env.update(kwargs.pop("extra_env", {}))  # type: ignore[arg-type]
    return harness.run(args, script=UNINSTALLER, extra_env=extra_env, **kwargs)  # type: ignore[arg-type]


@pytest.mark.unit
def test_remove_uv_without_marker_refuses(harness: InstallerHarness) -> None:
    """``--remove-uv`` with no ownership marker refuses and never touches uv."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")  # uv present but NOT AWF-owned (no marker)

    result = _run(harness, ["--remove-uv", "--yes"])

    assert result.returncode != 0
    assert "UV_REMOVAL_REFUSED_UNOWNED" in result.stderr
    # uv must not be removed when ownership is unproven.
    assert "uv tool uninstall" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_remove_uv_with_marker_and_yes_deletes_binaries(
    harness: InstallerHarness,
) -> None:
    """``--remove-uv --yes`` with the marker deletes the uv/uvx binaries and clears the marker."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    uv_bin_dir = harness.home / ".local" / "bin"
    uv_bin_dir.mkdir(parents=True, exist_ok=True)
    uv_bin = uv_bin_dir / "uv"
    uvx_bin = uv_bin_dir / "uvx"
    uv_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    uvx_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    marker = harness.write_uv_marker(uv_bin_dir=str(uv_bin_dir))

    result = _run(harness, ["--remove-uv", "--yes"])

    assert result.returncode == 0, result.stderr
    # The official uv has no `uv self uninstall`; removal deletes the binaries.
    assert "uv self uninstall" not in "\n".join(harness.calls())
    assert not uv_bin.exists()
    assert not uvx_bin.exists()
    # The proof-of-ownership marker is removed once uv is gone.
    assert not marker.exists()


@pytest.mark.unit
def test_remove_uv_dry_run_plans_without_mutation(harness: InstallerHarness) -> None:
    """``--remove-uv --dry-run`` plans the removal but deletes nothing and keeps the marker."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    uv_bin_dir = harness.home / ".local" / "bin"
    uv_bin_dir.mkdir(parents=True, exist_ok=True)
    uv_bin = uv_bin_dir / "uv"
    uv_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    marker = harness.write_uv_marker(uv_bin_dir=str(uv_bin_dir))

    result = _run(harness, ["--remove-uv", "--dry-run"])

    assert result.returncode == 0, result.stderr
    # The plan names the action; the binary and marker survive a dry run.
    assert "remove uv binaries" in result.stdout
    assert uv_bin.exists()
    assert marker.exists()


@pytest.mark.unit
def test_remove_uv_non_interactive_without_yes_requires_confirmation(
    harness: InstallerHarness,
) -> None:
    """With the marker present but no ``--yes`` (non-interactive), the run fails closed."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    marker = harness.write_uv_marker()

    result = _run(harness, ["--remove-uv", "--non-interactive"])

    assert result.returncode != 0
    assert "CONFIRMATION_REQUIRED" in result.stderr
    # Nothing ran and the marker is intact.
    assert "uv tool uninstall" not in "\n".join(harness.calls())
    assert marker.exists()


@pytest.mark.unit
def test_remove_uv_interactive_yes_runs(harness: InstallerHarness) -> None:
    """An affirmative interactive answer authorizes the marker-gated uv removal."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    uv_bin_dir = harness.home / ".local" / "bin"
    uv_bin_dir.mkdir(parents=True, exist_ok=True)
    uv_bin = uv_bin_dir / "uv"
    uv_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    marker = harness.write_uv_marker(uv_bin_dir=str(uv_bin_dir))

    result = _run(
        harness,
        ["--remove-uv"],
        extra_env={"AWF_UNINSTALL_FORCE_INTERACTIVE": "1"},
        stdin="y\n",
    )

    assert result.returncode == 0, result.stderr
    assert not uv_bin.exists()
    assert not marker.exists()


@pytest.mark.unit
def test_uv_preserved_when_remove_uv_absent(harness: InstallerHarness) -> None:
    """Without ``--remove-uv`` a default managed removal never touches uv itself.

    Even with the ownership marker present, the uninstaller removes only the
    managed package unless ``--remove-uv`` is explicitly given.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output=f"{PACKAGE} v0.1.0\n- awf\n")
    uv_bin_dir = harness.home / ".local" / "bin"
    uv_bin_dir.mkdir(parents=True, exist_ok=True)
    uv_bin = uv_bin_dir / "uv"
    uv_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    marker = harness.write_uv_marker(uv_bin_dir=str(uv_bin_dir))

    result = _run(harness, [])

    assert result.returncode == 0, result.stderr
    joined = "\n".join(harness.calls())
    assert f"uv tool uninstall {PACKAGE}" in joined
    # Without --remove-uv the uv binary and its ownership marker are left intact.
    assert uv_bin.exists()
    assert marker.exists()


@pytest.mark.unit
def test_managed_package_removed_before_uv_removal(
    harness: InstallerHarness,
) -> None:
    """``--remove-uv`` removes the uv-managed package *before* removing uv itself.

    Regression for the ordering bug: when the package is uv-managed and uv is
    AWF-bootstrapped, removing uv first drops uv from PATH, so the later
    ``uv tool uninstall`` cannot run and the still-installed package is misread as
    unmanaged (UNINSTALL_REFUSED_UNMANAGED) after uv is already gone. The package
    lane must therefore run while uv still exists; uv removal is last.

    The marker points uv's bin dir at the harness ``PATH`` dir holding the uv stub,
    so removing uv actually unhooks it from ``PATH``. The package lane still logging
    ``uv tool uninstall`` proves uv was present when it ran — i.e. removal came
    after the package lane, not before.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output=f"{PACKAGE} v0.1.0\n- awf\n")
    harness.add_pipx(list_output="")
    uv_stub = harness.bin_dir / "uv"
    marker = harness.write_uv_marker(uv_bin_dir=str(harness.bin_dir))

    result = _run(harness, ["--remove-uv", "--yes"])

    assert result.returncode == 0, result.stderr
    # The managed package was removed while uv still existed on PATH...
    assert f"uv tool uninstall {PACKAGE}" in "\n".join(harness.calls())
    # ...and uv itself was deleted afterward, clearing the ownership marker.
    assert not uv_stub.exists()
    assert not marker.exists()


@pytest.mark.unit
def test_remove_uv_binary_deletion_failure_preserves_marker(
    harness: InstallerHarness,
) -> None:
    """If deleting the uv binary fails, the run fails closed and keeps the marker.

    Regression for the swallowed-failure bug: a removal that cannot delete the uv
    binary must not report success and must not strand the marker, since deleting
    the marker would make a later ``--remove-uv`` refuse (no proof of ownership)
    and leave uv installed forever. We raise UV_REMOVAL_FAILED and keep the marker
    so the removal can be retried.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    uv_bin_dir = harness.home / "uvbin"
    uv_bin_dir.mkdir(parents=True, exist_ok=True)
    # A directory at the uv path makes `rm -f <dir>/uv` fail (Is a directory).
    (uv_bin_dir / "uv").mkdir()
    marker = harness.write_uv_marker(uv_bin_dir=str(uv_bin_dir))

    result = _run(harness, ["--remove-uv", "--yes"])

    assert result.returncode != 0
    assert "UV_REMOVAL_FAILED" in result.stderr
    # The marker survives so --remove-uv can retry rather than refusing forever.
    assert marker.exists()
