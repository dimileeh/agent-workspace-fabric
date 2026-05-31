"""Uninstall removes only AWF-managed installs and refuses unknown binaries."""

from __future__ import annotations

import pytest

from tests.unit.installer.conftest import InstallerHarness

PACKAGE = "agent-workspace-fabric"


def _list_output_with_trailing_bulk(prefix: str) -> str:
    """Return ``prefix`` followed by ~1 MiB of unrelated package manager output.

    ``uv_lists_package``/``pipx_lists_package`` detect a managed install by
    piping ``uv tool list``/``pipx list`` into ``grep``. The AWF entry sits in
    ``prefix`` (the first line(s)); the trailing bulk models the many other tools
    a real manager lists *after* it. With an early-exiting ``grep -q`` the match
    on the first line closes the pipe, leaving the producer mid-write; under
    ``set -o pipefail`` it takes SIGPIPE (141) and the pipeline reports the
    managed install as unmanaged. ~1 MiB comfortably exceeds the 64 KiB pipe
    buffer plus grep's read block, so the race fires deterministically. None of
    the filler lines match ``PACKAGE`` as a token, so they never produce a real
    match — only the trailing-write SIGPIPE matters here.
    """
    filler = "".join(f"other-tool-{i} v1.0.0\n" for i in range(50000))
    return prefix + filler


@pytest.mark.unit
def test_uninstall_managed_uv_install(harness: InstallerHarness) -> None:
    """A uv-managed install is removed via ``uv tool uninstall``."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output=f"{PACKAGE} v0.1.0\n- awf\n")

    result = harness.run(["--uninstall"])

    assert result.returncode == 0, result.stderr
    assert f"uv tool uninstall {PACKAGE}" in "\n".join(harness.calls())


@pytest.mark.unit
def test_uninstall_managed_pipx_install(harness: InstallerHarness) -> None:
    """A pipx-managed install is removed via ``pipx uninstall``."""
    harness.add_uname("Linux", "x86_64")
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
def test_uninstall_removes_both_managers_when_both_manage(
    harness: InstallerHarness,
) -> None:
    """When uv *and* pipx both manage the package, both copies are removed.

    A package installed by both managers is reported by ``uv tool list`` and
    ``pipx list`` at once. Stopping after the uv removal would leave the pipx
    copy installed while the run reports success, so ``awf`` could stay
    runnable. Both removal commands must run.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output=f"{PACKAGE} v0.1.0\n- awf\n")
    harness.add_pipx(list_output=f"package {PACKAGE} 0.1.0\n")

    result = harness.run(["--uninstall"])

    assert result.returncode == 0, result.stderr
    joined = "\n".join(harness.calls())
    assert f"uv tool uninstall {PACKAGE}" in joined
    assert f"pipx uninstall {PACKAGE}" in joined


@pytest.mark.unit
def test_uninstall_pipx_passes_install_dir_as_pipx_bin_dir(
    harness: InstallerHarness,
) -> None:
    """Uninstalling a pipx install honours ``--install-dir`` via ``PIPX_BIN_DIR``.

    ``install_pipx`` links console scripts into ``PIPX_BIN_DIR="$INSTALL_DIR"``.
    On uninstall, pipx only removes scripts from the bin dir it computes at that
    moment, so without re-exporting the install-time ``PIPX_BIN_DIR`` it deletes
    the venv but orphans ``${INSTALL_DIR}/awf`` while still exiting 0 — a
    still-runnable ``awf`` masked by a success exit. The uninstall must pass the
    same ``PIPX_BIN_DIR`` the matching install used.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    harness.add_pipx(list_output=f"package {PACKAGE} 0.1.0\n")

    result = harness.run(["--uninstall", "--install-dir", "/custom/bin"])

    assert result.returncode == 0, result.stderr
    joined = "\n".join(harness.calls())
    assert f"pipx uninstall {PACKAGE}" in joined
    # The removal ran with the install-time PIPX_BIN_DIR so the script linked
    # into the custom dir is removed, not orphaned behind a success exit.
    assert "pipx-uninstall-env PIPX_BIN_DIR=/custom/bin" in joined


@pytest.mark.unit
def test_uninstall_uv_passes_install_dir_as_uv_tool_bin_dir(
    harness: InstallerHarness,
) -> None:
    """Uninstalling a uv install honours ``--install-dir`` via ``UV_TOOL_BIN_DIR``.

    ``install_uv`` links the executable into ``UV_TOOL_BIN_DIR="$INSTALL_DIR"``.
    ``uv tool uninstall`` removes entry points from the bin dir it computes at
    uninstall time, so the install-time ``UV_TOOL_BIN_DIR`` must be re-exported or
    ``${INSTALL_DIR}/awf`` is orphaned while the run still exits 0 — the same
    defect the pipx path has.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output=f"{PACKAGE} v0.1.0\n- awf\n")
    harness.add_pipx(list_output="")

    result = harness.run(["--uninstall", "--install-dir", "/custom/bin"])

    assert result.returncode == 0, result.stderr
    joined = "\n".join(harness.calls())
    assert f"uv tool uninstall {PACKAGE}" in joined
    assert "uv-tool-uninstall-env UV_TOOL_BIN_DIR=/custom/bin" in joined


@pytest.mark.unit
def test_uninstall_without_install_dir_leaves_bin_dir_env_unset(
    harness: InstallerHarness,
) -> None:
    """Without ``--install-dir`` the uninstall must not force an empty bin dir.

    Exporting ``PIPX_BIN_DIR=`` / ``UV_TOOL_BIN_DIR=`` (empty) would point each
    manager at an empty bin dir instead of its normal default, breaking the
    common no-``--install-dir`` uninstall. With no override the variable must be
    left unset so each manager resolves its usual default bin dir — mirroring how
    ``install_uv``/``install_pipx`` only set the prefix when ``INSTALL_DIR`` is
    non-empty.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output=f"{PACKAGE} v0.1.0\n- awf\n")
    harness.add_pipx(list_output=f"package {PACKAGE} 0.1.0\n")

    result = harness.run(["--uninstall"])

    assert result.returncode == 0, result.stderr
    joined = "\n".join(harness.calls())
    assert "uv-tool-uninstall-env UV_TOOL_BIN_DIR=<unset>" in joined
    assert "pipx-uninstall-env PIPX_BIN_DIR=<unset>" in joined


@pytest.mark.unit
def test_uninstall_fails_when_pipx_removal_fails_after_uv_succeeds(
    harness: InstallerHarness,
) -> None:
    """A pipx removal failing after a successful uv removal fails the run.

    When both managers own the package the uv copy is removed first and pipx
    second. If pipx then fails, the uv copy is already gone while the pipx copy
    may remain, so the run must surface ``INSTALL_METHOD_FAILED`` (non-zero)
    rather than let a still-runnable ``awf`` hide behind a success exit.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output=f"{PACKAGE} v0.1.0\n- awf\n", uninstall_rc=0)
    harness.add_pipx(list_output=f"package {PACKAGE} 0.1.0\n", uninstall_rc=1)

    result = harness.run(["--uninstall"])

    assert result.returncode != 0
    assert "INSTALL_METHOD_FAILED" in result.stderr
    joined = "\n".join(harness.calls())
    # Both removals are attempted even though pipx ultimately fails.
    assert f"uv tool uninstall {PACKAGE}" in joined
    assert f"pipx uninstall {PACKAGE}" in joined


@pytest.mark.unit
def test_uninstall_still_attempts_pipx_when_uv_removal_fails(
    harness: InstallerHarness,
) -> None:
    """A uv removal failure must not short-circuit the pipx removal.

    Failing fast on the first manager would leave a pipx-managed copy untouched
    — the partial uninstall the dual-manager probe exists to prevent. Both
    removals must run; the run then exits non-zero because uv failed.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output=f"{PACKAGE} v0.1.0\n- awf\n", uninstall_rc=1)
    harness.add_pipx(list_output=f"package {PACKAGE} 0.1.0\n", uninstall_rc=0)

    result = harness.run(["--uninstall"])

    assert result.returncode != 0
    assert "INSTALL_METHOD_FAILED" in result.stderr
    joined = "\n".join(harness.calls())
    # The uv failure must not skip the pipx removal: both commands run.
    assert f"uv tool uninstall {PACKAGE}" in joined
    assert f"pipx uninstall {PACKAGE}" in joined


@pytest.mark.unit
def test_dry_run_uninstall_previews_both_managers_when_both_manage(
    harness: InstallerHarness,
) -> None:
    """``--dry-run --uninstall`` previews both removals when both manage it.

    The dry-run plan must enumerate every removal a real run would perform, so a
    package owned by both uv and pipx shows both planned commands while neither
    actually runs.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output=f"{PACKAGE} v0.1.0\n- awf\n")
    harness.add_pipx(list_output=f"package {PACKAGE} 0.1.0\n")

    result = harness.run(["--dry-run", "--uninstall"])

    assert result.returncode == 0, result.stderr
    joined = "\n".join(harness.calls())
    # Neither removal command may run under dry-run.
    assert f"uv tool uninstall {PACKAGE}" not in joined
    assert f"pipx uninstall {PACKAGE}" not in joined
    # Both planned removals must be previewed.
    assert f"uv tool uninstall {PACKAGE}" in result.stdout
    assert f"pipx uninstall {PACKAGE}" in result.stdout


@pytest.mark.unit
def test_uninstall_refuses_unmanaged_executable(harness: InstallerHarness) -> None:
    """An unmanaged awf on PATH is refused and never deleted."""
    harness.add_uname("Linux", "x86_64")
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
    harness.add_uname("Linux", "x86_64")
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
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="my-agent-workspace-fabric-fork v0.1.0\n- awf\n")
    harness.add_pipx(list_output="")

    result = harness.run(["--uninstall"])

    assert result.returncode == 0, result.stderr
    assert f"uv tool uninstall {PACKAGE}" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_uninstall_ignores_pipx_substring_fork(harness: InstallerHarness) -> None:
    """A similarly-named pipx package must not match PACKAGE as a substring."""
    harness.add_uname("Linux", "x86_64")
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
    harness.add_uname("Linux", "x86_64")
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
    harness.add_uname("Linux", "x86_64")
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
def test_dry_run_uninstall_refuses_unmanaged_binary_with_reason_token(
    harness: InstallerHarness,
) -> None:
    """``--dry-run --uninstall`` still refuses an unmanaged binary non-zero.

    Dry-run suppresses mutations (the uv/pipx removal commands), but the
    unmanaged-install refusal is a policy check, not a mutation: when neither
    uv nor pipx owns the package and a plain ``awf`` is on PATH, there is
    nothing to preview removing, so ``UNINSTALL_REFUSED_UNMANAGED`` fires and
    the binary is left untouched even under ``--dry-run``.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")  # uv reports no managed package
    harness.add_pipx(list_output="")  # pipx reports no managed package
    unmanaged = harness.add_awf()  # plain executable on PATH, not managed

    result = harness.run(["--dry-run", "--uninstall"])

    assert result.returncode != 0
    assert "UNINSTALL_REFUSED_UNMANAGED" in result.stderr
    # The refusal must not delete the binary, and dry-run must not run a removal.
    assert unmanaged.exists()
    joined = "\n".join(harness.calls())
    assert f"uv tool uninstall {PACKAGE}" not in joined
    assert f"pipx uninstall {PACKAGE}" not in joined


@pytest.mark.unit
def test_uninstall_managed_uv_install_with_large_tool_list(
    harness: InstallerHarness,
) -> None:
    """A uv-managed install is detected even when ``uv tool list`` is large.

    Regression for the SIGPIPE-under-pipefail false negative in
    ``uv_lists_package``: ``uv tool list`` prints the AWF entry first and then
    many more tools. An early-exiting ``grep -q`` would match the first line,
    close the pipe, and leave ``uv tool list`` taking SIGPIPE; under ``set -o
    pipefail`` the pipeline then exits 141 and ``uv_lists_package`` wrongly
    reports the install as unmanaged — so the uv copy is never removed and the
    unmanaged-refusal path fires instead. Draining the stream keeps detection
    correct, so the managed copy is uninstalled.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output=_list_output_with_trailing_bulk(f"{PACKAGE} v0.1.0\n- awf\n"))
    harness.add_pipx(list_output="")
    # A uv-managed install also links ``awf`` onto PATH; its presence means a
    # false "unmanaged" detection escalates to UNINSTALL_REFUSED_UNMANAGED, so the
    # regression fails loudly (non-zero) rather than silently no-opping.
    harness.add_awf()

    result = harness.run(["--uninstall"])

    assert result.returncode == 0, result.stderr
    assert f"uv tool uninstall {PACKAGE}" in "\n".join(harness.calls())


@pytest.mark.unit
def test_uninstall_managed_pipx_install_with_large_list(
    harness: InstallerHarness,
) -> None:
    """A pipx-managed install is detected even when ``pipx list`` is large.

    The pipx twin of the uv SIGPIPE regression: when the AWF entry is not the
    last line ``pipx list`` prints, an early-exiting ``grep -q`` SIGPIPEs pipx and
    the pipefail 141 status would make ``pipx_lists_package`` miss a genuinely
    managed install. Draining the stream keeps detection correct, so the pipx
    copy is removed.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    harness.add_pipx(list_output=_list_output_with_trailing_bulk(f"package {PACKAGE} 0.1.0\n"))
    harness.add_awf()

    result = harness.run(["--uninstall"])

    assert result.returncode == 0, result.stderr
    joined = "\n".join(harness.calls())
    assert f"pipx uninstall {PACKAGE}" in joined
    # uv reported nothing, so only the pipx removal runs.
    assert f"uv tool uninstall {PACKAGE}" not in joined


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
