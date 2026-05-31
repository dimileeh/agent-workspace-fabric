"""Regression test for non-OSError failures in plain-file credential writes.

Covers issue:4585275389 (PR #333): ``_write_secret_file`` must normalize *any*
write failure — not only ``OSError`` — to ``CredentialError`` and must never
orphan a temp file in the 0700 secrets directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import awf.host_setup.credentials as credentials
from awf.host_setup.credentials import (
    CREDENTIAL_BACKEND_UNAVAILABLE,
    CredentialError,
)


@pytest.mark.unit
def test_write_secret_file_wraps_non_oserror_and_cleans_up(tmp_path: Path) -> None:
    """Non-UTF-8 secret yields CredentialError, leaving no orphaned temp file.

    A lone surrogate is a valid ``str`` but cannot be encoded as UTF-8, so
    ``handle.write`` raises ``UnicodeEncodeError`` (a ``ValueError``, not an
    ``OSError``). Previously this bypassed the ``except OSError`` handler, leaving
    the 0600 temp file behind in the 0700 secrets dir and propagating a raw
    ``ValueError`` instead of the documented ``CredentialError``.
    """
    target = tmp_path / "secrets" / "github.secret"
    surrogate_secret = "tok-\ud800"

    with pytest.raises(CredentialError) as excinfo:
        credentials._write_secret_file(target, surrogate_secret)

    assert excinfo.value.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE
    # The wrapping must not leak the secret into the raised error.
    assert "\ud800" not in str(excinfo.value)
    assert "\ud800" not in repr(excinfo.value)
    # Neither the target nor an orphaned ".tmp" file may survive the failure.
    leftover = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert leftover == []
