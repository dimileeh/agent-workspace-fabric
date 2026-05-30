"""Uninstall removes only AWF-managed installs and refuses unknown binaries."""

from __future__ import annotations

import pytest

from tests.unit.installer.conftest import InstallerHarness

PACKAGE = "agent-workspace-fabric"


@pytest.mark.unit
def test_uninstall_managed_uv_install(harness: InstallerHarness) -> None:
    """A uv-managed install is removed via ``uv tool uninstall``."""
    harness.add_uv(list_output=f"{PACKAGE} v0.1.0\n- awf\n")

    result = harness.run(["--uninstall"])

    assert result.returncode == 0, result.stderr
    assert f"uv tool uninstall {PACKAGE}" in "\n".join(harness.calls())


@pytest.mark.unit
def test_uninstall_managed_pipx_install(harness: InstallerHarness) -> None:
    """A pipx-managed install is removed via ``pipx uninstall``."""
    harness.add_uv(list_output="")
    harness.add_pipx(list_output=f"package {PACKAGE} 0.1.0\n")

    result = harness.run(["--uninstall", "--method", "pipx"])

    assert result.returncode == 0, result.stderr
    assert f"pipx uninstall {PACKAGE}" in "\n".join(harness.calls())


@pytest.mark.unit
def test_uninstall_refuses_unmanaged_executable(harness: InstallerHarness) -> None:
    """An unmanaged awf on PATH is refused and never deleted."""
    harness.add_uv(list_output="")  # uv reports no managed package
    harness.add_pipx(list_output="")
    unmanaged = harness.add_awf()  # plain executable on PATH, not managed

    result = harness.run(["--uninstall"])

    assert result.returncode != 0
    assert "UNINSTALL_REFUSED_UNMANAGED" in result.stderr
    # The unmanaged binary must survive the refusal untouched.
    assert unmanaged.exists()
    assert f"uv tool uninstall {PACKAGE}" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_uninstall_with_nothing_installed_is_a_noop(harness: InstallerHarness) -> None:
    """With no managed package and no awf on PATH, uninstall is a clean no-op."""
    harness.add_uv(list_output="")
    harness.add_pipx(list_output="")

    result = harness.run(["--uninstall"])

    assert result.returncode == 0, result.stderr
    assert f"uv tool uninstall {PACKAGE}" not in "\n".join(harness.calls())
