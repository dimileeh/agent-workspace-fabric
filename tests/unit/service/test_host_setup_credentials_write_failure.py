"""Regression test for non-OSError failures in plain-file credential writes.

Covers issue:4585275389 (PR #333): ``_write_secret_file`` must normalize *any*
write failure — not only ``OSError`` — to ``CredentialError`` and must never
orphan a temp file in the 0700 secrets directory.
"""

from __future__ import annotations

import os
import stat
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


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="chmod hardening only runs on POSIX")
def test_write_secret_file_refuses_non_directory_secrets_dir(tmp_path: Path) -> None:
    """A ``secrets_dir`` that is a regular file is refused without being chmod'd.

    ``_mkdir_secure`` only walks/creates *missing* ancestors, so when the
    configured secrets dir already exists as a regular file (e.g. a
    ``~/.awf/secrets`` accidentally created as a file) the creation loop is
    skipped. The function must fail before applying the 0o700 directory
    hardening chmod — otherwise a doomed plain-file setup silently
    re-permissions an unrelated existing file before the secret-file open fails.
    """
    secrets_dir = tmp_path / "secrets"
    secrets_dir.write_text("pre-existing unrelated file")
    secrets_dir.chmod(0o644)
    before_mode = stat.S_IMODE(secrets_dir.stat().st_mode)
    target = secrets_dir / "github.secret"

    with pytest.raises(CredentialError) as excinfo:
        credentials._write_secret_file(target, "tok-value")

    assert excinfo.value.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE
    # The failed setup must not have re-permissioned the unrelated file...
    after_mode = stat.S_IMODE(secrets_dir.stat().st_mode)
    assert after_mode == before_mode == 0o644
    # ...nor touched its contents.
    assert secrets_dir.read_text() == "pre-existing unrelated file"
