"""Managed package removal for ``packaging/uninstall.sh`` (T21).

The hosted uninstaller's default lane (no purge/uv flags) is behaviorally equal
to ``install.sh --uninstall``: it removes an AWF-managed uv/pipx package, refuses
an unmanaged ``awf``, and no-ops when nothing is installed. These cases mirror
``test_install_sh_uninstall.py`` against the standalone uninstaller so the
mirrored removal helpers stay drift-free and the SIGPIPE-under-pipefail and
substring-fork hardening are re-proven for the new script.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.unit.installer.conftest import UNINSTALLER, InstallerHarness

PACKAGE = "agent-workspace-fabric"


def _run(harness: InstallerHarness, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run the uninstaller (always the ``UNINSTALLER`` script)."""
    return harness.run(args, script=UNINSTALLER)


def _list_output_with_trailing_bulk(prefix: str) -> str:
    """Return ``prefix`` followed by ~1 MiB of unrelated package manager output.

    Same SIGPIPE-under-pipefail regression driver as the installer suite: the
    AWF entry sits first, the trailing bulk models the many tools a manager lists
    after it, and ~1 MiB exceeds the pipe buffer so an early-exiting ``grep -q``
    would leave the producer taking SIGPIPE (141) and misreport the install as
    unmanaged. None of the filler lines match ``PACKAGE`` as a token.
    """
    filler = "".join(f"other-tool-{i} v1.0.0\n" for i in range(50000))
    return prefix + filler


@pytest.mark.unit
def test_default_lane_removes_managed_uv_install(harness: InstallerHarness) -> None:
    """A uv-managed install is removed via ``uv tool uninstall`` with no flags."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output=f"{PACKAGE} v0.1.0\n- awf\n")

    result = _run(harness, [])

    assert result.returncode == 0, result.stderr
    assert f"uv tool uninstall {PACKAGE}" in "\n".join(harness.calls())


@pytest.mark.unit
def test_default_lane_removes_managed_pipx_install(harness: InstallerHarness) -> None:
    """A pipx-managed install is removed via ``pipx uninstall`` (routing by discovery)."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    harness.add_pipx(list_output=f"package {PACKAGE} 0.1.0\n")

    result = _run(harness, [])

    assert result.returncode == 0, result.stderr
    joined = "\n".join(harness.calls())
    assert f"pipx uninstall {PACKAGE}" in joined
    assert f"uv tool uninstall {PACKAGE}" not in joined


@pytest.mark.unit
def test_removes_both_managers_when_both_manage(harness: InstallerHarness) -> None:
    """When uv *and* pipx both manage the package, both copies are removed."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output=f"{PACKAGE} v0.1.0\n- awf\n")
    harness.add_pipx(list_output=f"package {PACKAGE} 0.1.0\n")

    result = _run(harness, [])

    assert result.returncode == 0, result.stderr
    joined = "\n".join(harness.calls())
    assert f"uv tool uninstall {PACKAGE}" in joined
    assert f"pipx uninstall {PACKAGE}" in joined


@pytest.mark.unit
def test_uv_removal_passes_install_dir_as_uv_tool_bin_dir(
    harness: InstallerHarness,
) -> None:
    """``--install-dir`` is forwarded to ``uv tool uninstall`` via ``UV_TOOL_BIN_DIR``."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output=f"{PACKAGE} v0.1.0\n- awf\n")
    harness.add_pipx(list_output="")

    result = _run(harness, ["--install-dir", "/custom/bin"])

    assert result.returncode == 0, result.stderr
    joined = "\n".join(harness.calls())
    assert f"uv tool uninstall {PACKAGE}" in joined
    assert "uv-tool-uninstall-env UV_TOOL_BIN_DIR=/custom/bin" in joined


@pytest.mark.unit
def test_pipx_removal_passes_install_dir_as_pipx_bin_dir(
    harness: InstallerHarness,
) -> None:
    """``--install-dir`` is forwarded to ``pipx uninstall`` via ``PIPX_BIN_DIR``."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    harness.add_pipx(list_output=f"package {PACKAGE} 0.1.0\n")

    result = _run(harness, ["--install-dir", "/custom/bin"])

    assert result.returncode == 0, result.stderr
    joined = "\n".join(harness.calls())
    assert f"pipx uninstall {PACKAGE}" in joined
    assert "pipx-uninstall-env PIPX_BIN_DIR=/custom/bin" in joined


@pytest.mark.unit
def test_without_install_dir_leaves_bin_dir_env_unset(
    harness: InstallerHarness,
) -> None:
    """Without ``--install-dir`` the removal must not force an empty bin dir."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output=f"{PACKAGE} v0.1.0\n- awf\n")
    harness.add_pipx(list_output=f"package {PACKAGE} 0.1.0\n")

    result = _run(harness, [])

    assert result.returncode == 0, result.stderr
    joined = "\n".join(harness.calls())
    assert "uv-tool-uninstall-env UV_TOOL_BIN_DIR=<unset>" in joined
    assert "pipx-uninstall-env PIPX_BIN_DIR=<unset>" in joined


@pytest.mark.unit
def test_fails_when_pipx_removal_fails_after_uv_succeeds(
    harness: InstallerHarness,
) -> None:
    """A pipx removal failing after a successful uv removal fails the run non-zero."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output=f"{PACKAGE} v0.1.0\n- awf\n", uninstall_rc=0)
    harness.add_pipx(list_output=f"package {PACKAGE} 0.1.0\n", uninstall_rc=1)

    result = _run(harness, [])

    assert result.returncode != 0
    assert "INSTALL_METHOD_FAILED" in result.stderr
    joined = "\n".join(harness.calls())
    assert f"uv tool uninstall {PACKAGE}" in joined
    assert f"pipx uninstall {PACKAGE}" in joined


@pytest.mark.unit
def test_still_attempts_pipx_when_uv_removal_fails(harness: InstallerHarness) -> None:
    """A uv removal failure must not short-circuit the pipx removal."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output=f"{PACKAGE} v0.1.0\n- awf\n", uninstall_rc=1)
    harness.add_pipx(list_output=f"package {PACKAGE} 0.1.0\n", uninstall_rc=0)

    result = _run(harness, [])

    assert result.returncode != 0
    assert "INSTALL_METHOD_FAILED" in result.stderr
    joined = "\n".join(harness.calls())
    assert f"uv tool uninstall {PACKAGE}" in joined
    assert f"pipx uninstall {PACKAGE}" in joined


@pytest.mark.unit
def test_refuses_unmanaged_executable(harness: InstallerHarness) -> None:
    """An unmanaged awf on PATH is refused and never deleted."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    harness.add_pipx(list_output="")
    unmanaged = harness.add_awf()

    result = _run(harness, [])

    assert result.returncode != 0
    assert "UNINSTALL_REFUSED_UNMANAGED" in result.stderr
    assert unmanaged.exists()
    assert f"uv tool uninstall {PACKAGE}" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_nothing_installed_is_a_noop(harness: InstallerHarness) -> None:
    """With no managed package and no awf on PATH, the run is a clean no-op."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    harness.add_pipx(list_output="")

    result = _run(harness, [])

    assert result.returncode == 0, result.stderr
    joined = "\n".join(harness.calls())
    assert f"uv tool uninstall {PACKAGE}" not in joined
    assert f"pipx uninstall {PACKAGE}" not in joined


@pytest.mark.unit
def test_ignores_uv_substring_fork(harness: InstallerHarness) -> None:
    """A similarly-named uv tool must not match PACKAGE as a substring."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="my-agent-workspace-fabric-fork v0.1.0\n- awf\n")
    harness.add_pipx(list_output="")

    result = _run(harness, [])

    assert result.returncode == 0, result.stderr
    assert f"uv tool uninstall {PACKAGE}" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_ignores_pipx_substring_fork(harness: InstallerHarness) -> None:
    """A similarly-named pipx package must not match PACKAGE as a substring."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    harness.add_pipx(list_output="package my-agent-workspace-fabric-fork 0.1.0\n")

    result = _run(harness, [])

    assert result.returncode == 0, result.stderr
    assert f"pipx uninstall {PACKAGE}" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_dry_run_previews_both_managers_without_mutation(
    harness: InstallerHarness,
) -> None:
    """``--dry-run`` previews both removals when both manage it, running neither."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output=f"{PACKAGE} v0.1.0\n- awf\n")
    harness.add_pipx(list_output=f"package {PACKAGE} 0.1.0\n")

    result = _run(harness, ["--dry-run"])

    assert result.returncode == 0, result.stderr
    joined = "\n".join(harness.calls())
    assert f"uv tool uninstall {PACKAGE}" not in joined
    assert f"pipx uninstall {PACKAGE}" not in joined
    assert f"uv tool uninstall {PACKAGE}" in result.stdout
    assert f"pipx uninstall {PACKAGE}" in result.stdout


@pytest.mark.unit
def test_dry_run_refuses_unmanaged_binary_with_reason_token(
    harness: InstallerHarness,
) -> None:
    """``--dry-run`` still refuses an unmanaged binary non-zero (policy, not mutation)."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    harness.add_pipx(list_output="")
    unmanaged = harness.add_awf()

    result = _run(harness, ["--dry-run"])

    assert result.returncode != 0
    assert "UNINSTALL_REFUSED_UNMANAGED" in result.stderr
    assert unmanaged.exists()


@pytest.mark.unit
def test_managed_uv_install_with_large_tool_list(harness: InstallerHarness) -> None:
    """A uv-managed install is detected even when ``uv tool list`` is large."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output=_list_output_with_trailing_bulk(f"{PACKAGE} v0.1.0\n- awf\n"))
    harness.add_pipx(list_output="")
    harness.add_awf()

    result = _run(harness, [])

    assert result.returncode == 0, result.stderr
    assert f"uv tool uninstall {PACKAGE}" in "\n".join(harness.calls())


@pytest.mark.unit
def test_managed_pipx_install_with_large_list(harness: InstallerHarness) -> None:
    """A pipx-managed install is detected even when ``pipx list`` is large."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    harness.add_pipx(list_output=_list_output_with_trailing_bulk(f"package {PACKAGE} 0.1.0\n"))
    harness.add_awf()

    result = _run(harness, [])

    assert result.returncode == 0, result.stderr
    joined = "\n".join(harness.calls())
    assert f"pipx uninstall {PACKAGE}" in joined
    assert f"uv tool uninstall {PACKAGE}" not in joined


@pytest.mark.unit
def test_aborts_on_unsupported_platform_before_mutation(
    harness: InstallerHarness,
) -> None:
    """An unsupported platform aborts before touching any managed install."""
    harness.add_uname("Windows_NT", "x86_64")
    harness.add_uv(list_output=f"{PACKAGE} v0.1.0\n- awf\n")
    harness.add_pipx(list_output="")

    result = _run(harness, [])

    assert result.returncode != 0
    assert "UNSUPPORTED_PLATFORM" in result.stderr
    joined = "\n".join(harness.calls())
    assert f"uv tool uninstall {PACKAGE}" not in joined
    assert f"pipx uninstall {PACKAGE}" not in joined


@pytest.mark.unit
def test_managed_removal_not_gated_by_yes(harness: InstallerHarness) -> None:
    """Managed package removal alone is not ``--yes`` gated (parity with install.sh).

    Only the new destructive filesystem lanes (state/config purge, uv removal)
    require ``--yes``/confirmation. A plain managed removal under
    ``--non-interactive`` without ``--yes`` must still succeed.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output=f"{PACKAGE} v0.1.0\n- awf\n")
    harness.add_pipx(list_output="")

    result = _run(harness, ["--non-interactive"])

    assert result.returncode == 0, result.stderr
    assert f"uv tool uninstall {PACKAGE}" in "\n".join(harness.calls())
