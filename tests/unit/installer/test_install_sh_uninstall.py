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

    # ``--method`` is intentionally ignored during ``--uninstall``: uninstall_awf
    # always probes uv then pipx by actual presence, never by the flag. It is
    # passed here only to prove that routing follows discovery (uv empty -> pipx
    # owns the removal), not the requested method.
    result = harness.run(["--uninstall", "--method", "pipx"])

    assert result.returncode == 0, result.stderr
    joined = "\n".join(harness.calls())
    assert f"pipx uninstall {PACKAGE}" in joined
    # The pipx path is exclusive: the uv uninstall command must not also run.
    assert f"uv tool uninstall {PACKAGE}" not in joined


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
    # A clean no-op must invoke neither uninstall command.
    joined = "\n".join(harness.calls())
    assert f"uv tool uninstall {PACKAGE}" not in joined
    assert f"pipx uninstall {PACKAGE}" not in joined


@pytest.mark.unit
def test_uninstall_ignores_uv_substring_fork(harness: InstallerHarness) -> None:
    """A similarly-named uv tool must not match PACKAGE as a substring."""
    # Only forks are present; the real package is NOT managed by uv. A substring
    # match would wrongly run ``uv tool uninstall agent-workspace-fabric``.
    harness.add_uv(list_output="my-agent-workspace-fabric-fork v0.1.0\n- awf\n")
    harness.add_pipx(list_output="")

    result = harness.run(["--uninstall"])

    assert result.returncode == 0, result.stderr
    assert f"uv tool uninstall {PACKAGE}" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_uninstall_ignores_pipx_substring_fork(harness: InstallerHarness) -> None:
    """A similarly-named pipx package must not match PACKAGE as a substring."""
    harness.add_uv(list_output="")
    harness.add_pipx(list_output="package my-agent-workspace-fabric-fork 0.1.0\n")

    result = harness.run(["--uninstall"])

    assert result.returncode == 0, result.stderr
    assert f"pipx uninstall {PACKAGE}" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_dry_run_uninstall_previews_uv_removal_without_mutation(
    harness: InstallerHarness,
) -> None:
    """``--dry-run --uninstall`` plans the uv removal but never invokes it."""
    harness.add_uv(list_output=f"{PACKAGE} v0.1.0\n- awf\n")

    result = harness.run(["--dry-run", "--uninstall"])

    assert result.returncode == 0, result.stderr
    # The real removal command must not run under dry-run.
    assert f"uv tool uninstall {PACKAGE}" not in "\n".join(harness.calls())
    # The plan still explains the action a real run would take.
    assert f"uv tool uninstall {PACKAGE}" in result.stdout


@pytest.mark.unit
def test_dry_run_uninstall_previews_pipx_removal_without_mutation(
    harness: InstallerHarness,
) -> None:
    """``--dry-run --uninstall`` plans the pipx removal but never invokes it."""
    harness.add_uv(list_output="")  # uv reports no managed package
    harness.add_pipx(list_output=f"package {PACKAGE} 0.1.0\n")

    result = harness.run(["--dry-run", "--uninstall"])

    assert result.returncode == 0, result.stderr
    joined = "\n".join(harness.calls())
    # Neither manager's removal command may run under dry-run.
    assert f"pipx uninstall {PACKAGE}" not in joined
    assert f"uv tool uninstall {PACKAGE}" not in joined
    # The plan reflects the discovered manager (pipx), not the install default.
    assert f"pipx uninstall {PACKAGE}" in result.stdout


@pytest.mark.unit
def test_uninstall_aborts_on_unsupported_platform_before_mutation(
    harness: InstallerHarness,
) -> None:
    """``--uninstall`` honours the platform guard before touching any install.

    The trust contract promises an unsupported platform aborts before any
    mutation. A managed uv install is present, so without the guard the
    uninstall path would invoke ``uv tool uninstall`` instead of aborting.
    """
    harness.add_uname("Windows_NT", "x86_64")
    harness.add_uv(list_output=f"{PACKAGE} v0.1.0\n- awf\n")
    harness.add_pipx(list_output="")

    result = harness.run(["--uninstall"])

    assert result.returncode != 0
    assert "UNSUPPORTED_PLATFORM" in result.stderr
    joined = "\n".join(harness.calls())
    assert f"uv tool uninstall {PACKAGE}" not in joined
    assert f"pipx uninstall {PACKAGE}" not in joined
