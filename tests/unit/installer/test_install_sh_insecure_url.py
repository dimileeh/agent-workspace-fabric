"""Plain-HTTP sources are rejected before any install mutation.

The sha256 gate proves the downloaded wheel matches the manifest, but it cannot
detect a *substituted* manifest. A manifest fetched over plain ``http://`` (or a
manifest whose artifact ``url`` is ``http://``) lets a network-adjacent attacker
swap both the manifest and a matching-sha256 malicious wheel, so the installer
refuses the insecure scheme with the ``INSECURE_URL`` reason token before it
mutates the user's environment. ``https://``/``file://``/local paths stay
allowed.
"""

from __future__ import annotations

import pytest

from tests.unit.installer.conftest import InstallerHarness


@pytest.mark.unit
def test_http_manifest_url_rejected_before_mutation(harness: InstallerHarness) -> None:
    """An ``http://`` manifest source aborts with INSECURE_URL, never installing."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()

    result = harness.run(
        [],
        extra_env={"AWF_INSTALL_MANIFEST": "http://example.invalid/awf-install-manifest.json"},
    )

    assert result.returncode != 0
    assert "INSECURE_URL" in result.stderr
    # The insecure scheme is refused before any uv/pipx mutation runs.
    assert "uv tool install" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_http_artifact_url_rejected_before_checksum(harness: InstallerHarness) -> None:
    """A file:// manifest pointing its wheel url at http:// is refused at download."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    wheel, digest = harness.write_wheel()
    manifest = harness.write_manifest(
        wheel=wheel,
        sha256=digest,
        wheel_url="http://example.invalid/agent_workspace_fabric-0.1.0-py3-none-any.whl",
    )

    result = harness.run([], manifest=manifest)

    assert result.returncode != 0
    assert "INSECURE_URL" in result.stderr
    # Refused at the artifact download, before checksum verification or install.
    joined = "\n".join(harness.calls())
    assert "uv tool install" not in joined
    assert "pipx install" not in joined


@pytest.mark.unit
def test_https_manifest_scheme_is_not_treated_as_insecure(harness: InstallerHarness) -> None:
    """An ``https://`` source is not refused for scheme (it fails only on fetch)."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()

    result = harness.run(
        [],
        extra_env={"AWF_INSTALL_MANIFEST": "https://example.invalid/awf-install-manifest.json"},
    )

    assert result.returncode != 0
    # https is a trusted scheme: the run fails to *reach* the host, not because
    # the scheme was rejected as insecure.
    assert "INSECURE_URL" not in result.stderr
    assert "MANIFEST_UNAVAILABLE" in result.stderr
