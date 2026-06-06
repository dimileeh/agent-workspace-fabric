"""Managed-removal parity between ``uninstall.sh`` and ``install.sh --uninstall``.

``uninstall.sh`` embeds the managed uv/pipx removal helpers mirrored from
``install.sh`` (a hosted ``curl | bash`` uninstaller cannot ``source`` a sibling
library). This light parity guard runs the same scenarios through both scripts
and asserts identical outcomes, so the mirrored helpers cannot silently drift.
"""

from __future__ import annotations

import pytest

from tests.unit.installer.conftest import INSTALLER, UNINSTALLER, InstallerHarness

PACKAGE = "agent-workspace-fabric"


@pytest.mark.unit
def test_unmanaged_refusal_matches_installer(harness: InstallerHarness) -> None:
    """Both scripts refuse an unmanaged ``awf`` with the same token and exit code."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    harness.add_pipx(list_output="")
    harness.add_awf()

    via_installer = harness.run(["--uninstall"], script=INSTALLER)
    via_uninstaller = harness.run([], script=UNINSTALLER)

    assert via_installer.returncode == via_uninstaller.returncode
    assert via_installer.returncode != 0
    assert "UNINSTALL_REFUSED_UNMANAGED" in via_installer.stderr
    assert "UNINSTALL_REFUSED_UNMANAGED" in via_uninstaller.stderr


@pytest.mark.unit
def test_managed_uv_removal_matches_installer(harness: InstallerHarness) -> None:
    """Both scripts remove a uv-managed install with the same ``uv tool uninstall``."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output=f"{PACKAGE} v0.1.0\n- awf\n")
    harness.add_pipx(list_output="")

    via_installer = harness.run(["--uninstall"], script=INSTALLER)
    assert via_installer.returncode == 0, via_installer.stderr
    assert f"uv tool uninstall {PACKAGE}" in "\n".join(harness.calls())

    # Reset the recorded calls between the two runs.
    harness.log_file.write_text("", encoding="utf-8")

    via_uninstaller = harness.run([], script=UNINSTALLER)
    assert via_uninstaller.returncode == 0, via_uninstaller.stderr
    assert f"uv tool uninstall {PACKAGE}" in "\n".join(harness.calls())


@pytest.mark.unit
def test_nothing_installed_noop_matches_installer(harness: InstallerHarness) -> None:
    """A clean no-op exits 0 under both scripts when nothing is installed."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    harness.add_pipx(list_output="")

    via_installer = harness.run(["--uninstall"], script=INSTALLER)
    via_uninstaller = harness.run([], script=UNINSTALLER)

    assert via_installer.returncode == 0, via_installer.stderr
    assert via_uninstaller.returncode == 0, via_uninstaller.stderr
