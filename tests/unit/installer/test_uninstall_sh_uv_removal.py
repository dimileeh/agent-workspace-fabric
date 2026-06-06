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
    # uv must not be self-uninstalled when ownership is unproven.
    assert "uv self uninstall" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_remove_uv_with_marker_and_yes_runs_self_uninstall(
    harness: InstallerHarness,
) -> None:
    """``--remove-uv --yes`` with the marker runs ``uv self uninstall`` and clears the marker."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    marker = harness.write_uv_marker()

    result = _run(harness, ["--remove-uv", "--yes"])

    assert result.returncode == 0, result.stderr
    assert "uv self uninstall" in "\n".join(harness.calls())
    # The proof-of-ownership marker is removed once uv is gone.
    assert not marker.exists()


@pytest.mark.unit
def test_remove_uv_dry_run_plans_without_mutation(harness: InstallerHarness) -> None:
    """``--remove-uv --dry-run`` plans the removal but runs nothing and keeps the marker."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    marker = harness.write_uv_marker()

    result = _run(harness, ["--remove-uv", "--dry-run"])

    assert result.returncode == 0, result.stderr
    assert "uv self uninstall" not in "\n".join(harness.calls())
    # The plan names the action; the marker survives a dry run.
    assert "uv self uninstall" in result.stdout
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
    assert "uv self uninstall" not in "\n".join(harness.calls())
    assert marker.exists()


@pytest.mark.unit
def test_remove_uv_interactive_yes_runs(harness: InstallerHarness) -> None:
    """An affirmative interactive answer authorizes the marker-gated uv removal."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="")
    marker = harness.write_uv_marker()

    result = _run(
        harness,
        ["--remove-uv"],
        extra_env={"AWF_UNINSTALL_FORCE_INTERACTIVE": "1"},
        stdin="y\n",
    )

    assert result.returncode == 0, result.stderr
    assert "uv self uninstall" in "\n".join(harness.calls())
    assert not marker.exists()


@pytest.mark.unit
def test_uv_preserved_when_remove_uv_absent(harness: InstallerHarness) -> None:
    """Without ``--remove-uv`` a default managed removal never touches uv itself.

    Even with the ownership marker present, the uninstaller removes only the
    managed package unless ``--remove-uv`` is explicitly given.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output=f"{PACKAGE} v0.1.0\n- awf\n")
    marker = harness.write_uv_marker()

    result = _run(harness, [])

    assert result.returncode == 0, result.stderr
    joined = "\n".join(harness.calls())
    assert f"uv tool uninstall {PACKAGE}" in joined
    assert "uv self uninstall" not in joined
    assert marker.exists()


@pytest.mark.unit
def test_managed_package_removed_before_uv_self_uninstall(
    harness: InstallerHarness,
) -> None:
    """``--remove-uv`` removes the uv-managed package *before* removing uv itself.

    Regression for the ordering bug: when the package is uv-managed and uv is
    AWF-bootstrapped, running ``uv self uninstall`` first drops uv from PATH, so
    the later ``uv tool uninstall`` cannot run and the still-installed package is
    misread as unmanaged (UNINSTALL_REFUSED_UNMANAGED) after uv is already gone.
    The package lane must therefore run while uv still exists; uv removal is last.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output=f"{PACKAGE} v0.1.0\n- awf\n")
    harness.add_pipx(list_output="")
    marker = harness.write_uv_marker()

    result = _run(harness, ["--remove-uv", "--yes"])

    assert result.returncode == 0, result.stderr
    calls = harness.calls()
    tool_uninstall = next(i for i, c in enumerate(calls) if f"uv tool uninstall {PACKAGE}" in c)
    self_uninstall = next(i for i, c in enumerate(calls) if "uv self uninstall" in c)
    # The managed package is removed before uv self-uninstalls (which would strand it).
    assert tool_uninstall < self_uninstall
    assert not marker.exists()


@pytest.mark.unit
def test_remove_uv_self_uninstall_failure_still_clears_marker(
    harness: InstallerHarness,
) -> None:
    """A non-zero ``uv self uninstall`` warns but the run succeeds and clears the marker.

    The contract under test is the marker gate, not uv's internals: a uv that
    reports a problem uninstalling itself should not strand the marker or fail the
    run, since the marker is AWF's own ownership record.
    """
    harness.add_uname("Linux", "x86_64")
    harness.add_uv(list_output="", self_uninstall_rc=1)
    marker = harness.write_uv_marker()

    result = _run(harness, ["--remove-uv", "--yes"])

    assert result.returncode == 0, result.stderr
    assert "uv self uninstall" in "\n".join(harness.calls())
    assert not marker.exists()
