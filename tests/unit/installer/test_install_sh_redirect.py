"""HTTPS downloads stay on HTTPS across redirects.

The installer refuses plain ``http://`` *sources* (``INSECURE_URL``), but that
guard only inspects the initial scheme. ``curl -fsSL`` follows redirects (``-L``)
and ``wget`` follows them by default, so an ``https://`` source that 30x-redirects
to ``http://`` could fetch manifest or wheel bytes over plain HTTP without ever
re-entering the guard — re-opening the substituted-manifest hole the guard
exists to close. The downloader invocations therefore pin every redirect hop to
HTTPS (``curl --proto/--proto-redir =https``, ``wget --https-only``), which still
follows legitimate ``https://`` → ``https://`` CDN redirects but refuses any
downgrade to plain HTTP.
"""

from __future__ import annotations

import pytest

from tests.unit.installer.conftest import INSTALLER, InstallerHarness


@pytest.mark.unit
def test_curl_download_pins_protocol_to_https_across_redirects(
    harness: InstallerHarness,
) -> None:
    """curl is invoked with ``--proto``/``--proto-redir =https`` for https sources."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    # Record curl's argv, then fail the fetch so the run aborts at manifest
    # resolution before any environment mutation.
    harness.add_curl(rc=1)

    result = harness.run(
        [],
        extra_env={"AWF_INSTALL_MANIFEST": "https://example.test/awf-install-manifest.json"},
    )

    assert result.returncode != 0
    curl_calls = [c for c in harness.calls() if c.startswith("curl ")]
    assert curl_calls, harness.calls()
    # The initial request and every redirect hop are pinned to https.
    assert any("--proto =https" in c for c in curl_calls), curl_calls
    assert any("--proto-redir =https" in c for c in curl_calls), curl_calls


@pytest.mark.unit
def test_downloaders_refuse_http_redirect_downgrade_in_source() -> None:
    """Both downloader branches carry the https redirect pins in the script.

    The curl branch is exercised behaviourally above; the wget fallback only runs
    when curl is absent, which the hermetic harness (system curl always on PATH)
    cannot force, so its hardening is asserted statically here. If either pin is
    dropped the redirect-downgrade hole reopens, so this guards both.
    """
    text = INSTALLER.read_text(encoding="utf-8")
    assert "--proto '=https'" in text
    assert "--proto-redir '=https'" in text
    assert "--https-only" in text
