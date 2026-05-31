"""Regression test for non-OSError failures in plain-file credential writes.

Covers issue:4585275389 (PR #333): ``_write_secret_file`` must normalize *any*
write failure — not only ``OSError`` — to ``CredentialError`` and must never
orphan a temp file in the 0700 secrets directory. Also pins the symlink-refusal
hardening for ``_mkdir_secure`` (a pre-planted symlink at the secrets dir or an
ancestor must not redirect secret writes to an attacker-controlled target).
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
    CredentialRequest,
    PlainFileCredentialBackend,
)
from tests.unit.service.test_host_setup_credentials_parts._helpers import (
    _FAKE_TOKEN,
    _HEADLESS_LINUX,
    _secret,
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


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="symlink semantics are POSIX-specific")
def test_write_secret_file_refuses_symlinked_secrets_dir(tmp_path: Path) -> None:
    """A secrets dir that is a symlink is refused; the link target is untouched.

    A pre-planted ``~/.awf/secrets -> /attacker`` symlink must not redirect the
    hardening chmod or the secret write into the attacker-controlled target.
    ``_mkdir_secure`` checks ``is_symlink`` (``lstat``, non-following) where the
    ``exists``/``is_dir`` probes would silently follow the link, so it refuses the
    symlinked leaf before the chmod and before any temp/secret file is opened.
    """
    attacker_dir = tmp_path / "attacker"
    attacker_dir.mkdir()
    attacker_dir.chmod(0o755)
    secrets_link = tmp_path / "secrets"
    secrets_link.symlink_to(attacker_dir)
    target = secrets_link / "github.secret"

    with pytest.raises(CredentialError) as excinfo:
        credentials._write_secret_file(target, "tok-value")

    assert excinfo.value.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE
    # The secret never landed inside the link target...
    assert list(attacker_dir.iterdir()) == []
    # ...and the target's permissions were not hardened to 0o700 through the link.
    assert stat.S_IMODE(attacker_dir.stat().st_mode) == 0o755
    assert "tok-value" not in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="symlink semantics are POSIX-specific")
def test_write_secret_file_refuses_symlinked_ancestor(tmp_path: Path) -> None:
    """A symlinked *ancestor* of the secrets dir is refused before creation.

    If ``~/.awf`` is a symlink to an attacker dir and ``~/.awf/secrets`` does not
    yet exist, the 0700 tree (and the secret) would otherwise be created inside
    the link target. ``_mkdir_secure`` refuses once it reaches the symlinked
    nearest existing ancestor rather than planting the secrets dir there.
    """
    attacker_dir = tmp_path / "attacker"
    attacker_dir.mkdir()
    parent_link = tmp_path / "awf"
    parent_link.symlink_to(attacker_dir)
    target = parent_link / "secrets" / "github.secret"

    with pytest.raises(CredentialError) as excinfo:
        credentials._write_secret_file(target, "tok-value")

    assert excinfo.value.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE
    # No secrets subdir (and no secret) was created inside the link target.
    assert list(attacker_dir.iterdir()) == []
    assert "tok-value" not in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="symlink semantics are POSIX-specific")
def test_write_secret_file_refuses_preexisting_dir_under_symlinked_ancestor(
    tmp_path: Path,
) -> None:
    """A *pre-created* secrets dir under a symlinked ancestor is refused.

    Regression for PRRT_kwDOSJAM6s6F777j: when the configured secrets dir already
    exists, the creation loop in ``_mkdir_secure`` runs zero iterations, so the
    earlier guard only ever ``lstat``-checked the leaf — which follows an
    intermediate ``~/.awf -> /attacker`` symlink and sees a perfectly ordinary
    directory at ``/attacker/secrets``. The symlinked ancestor was never walked,
    so the hardening chmod and every subsequent secret write landed under the
    attacker-controlled link target, bypassing the ancestor check the *create*
    path already applied. The guard must walk the existing ancestor chain too.
    """
    attacker_dir = tmp_path / "attacker"
    attacker_dir.mkdir()
    # Pre-create the secrets dir *inside* the link target so it is reachable only
    # by traversing the symlinked ancestor below — the exists-path the create
    # path's ``probe`` walk never reached.
    planted_secrets = attacker_dir / "secrets"
    planted_secrets.mkdir()
    planted_secrets.chmod(0o755)
    parent_link = tmp_path / "awf"
    parent_link.symlink_to(attacker_dir)
    target = parent_link / "secrets" / "github.secret"

    with pytest.raises(CredentialError) as excinfo:
        credentials._write_secret_file(target, "tok-value")

    assert excinfo.value.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE
    # The secret never landed in the pre-existing dir under the link target...
    assert list(planted_secrets.iterdir()) == []
    # ...and its permissions were not hardened to 0o700 through the link.
    assert stat.S_IMODE(planted_secrets.stat().st_mode) == 0o755
    assert "tok-value" not in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="symlink semantics are POSIX-specific")
def test_write_secret_file_refuses_preexisting_dir_under_symlinked_grandparent(
    tmp_path: Path,
) -> None:
    """A pre-created secrets dir below a *non-immediate* symlinked ancestor is refused.

    Regression for PRRT_kwDOSJAM6s6F8HOj: the earlier guard walked the existing
    ancestor chain but stopped at the *nearest* pre-existing directory, found via
    ``exists()`` which follows symlinks. ``is_symlink()`` (``lstat``) only inspects
    the final component of the path it is handed, so a symlinked *grandparent*
    above an existing descendant is never caught: with ``<tmp>/awf -> <tmp>/attacker``
    and the whole tree pre-created at ``<tmp>/attacker/profile/secrets``, probing
    ``<tmp>/awf/profile`` finds ``is_symlink()`` false (the link is an intermediate
    component there) and ``exists()`` true, so the walk broke before ever
    ``lstat``-ing ``<tmp>/awf`` itself. The hardening chmod and every secret write
    then followed the link into the attacker tree. The walk must continue checking
    ancestors past an existing descendant, all the way to the filesystem root.
    """
    attacker_dir = tmp_path / "attacker"
    # Pre-create the full managed tree *inside* the link target so every component
    # of the configured path "exists" only by traversing the symlinked grandparent
    # — the exists-path the nearest-ancestor walk stopped short of inspecting.
    planted_secrets = attacker_dir / "profile" / "secrets"
    planted_secrets.mkdir(parents=True)
    planted_secrets.chmod(0o755)
    grandparent_link = tmp_path / "awf"
    grandparent_link.symlink_to(attacker_dir)
    target = grandparent_link / "profile" / "secrets" / "github.secret"

    with pytest.raises(CredentialError) as excinfo:
        credentials._write_secret_file(target, "tok-value")

    assert excinfo.value.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE
    # The secret never landed in the pre-existing dir under the link target...
    assert list(planted_secrets.iterdir()) == []
    # ...and its permissions were not hardened to 0o700 through the link.
    assert stat.S_IMODE(planted_secrets.stat().st_mode) == 0o755
    assert "tok-value" not in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="symlink semantics are POSIX-specific")
def test_write_secret_file_rejects_symlink_racing_into_mkdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink planted *between* the lstat walk and the create is refused.

    Regression for PRRT_kwDOSJAM6s6F8Civ: the symlink walk verifies each missing
    component is absent and not a link, but the subsequent create is a separate
    syscall. With ``mkdir(..., exist_ok=True)`` an attacker who plants a
    ``component -> /attacker`` symlink in that TOCTOU window wins silently —
    ``mkdir`` raises ``FileExistsError`` and ``exist_ok`` swallows it because the
    follow-up ``is_dir()`` follows the link to a real directory, so the hardening
    chmod and every secret write then land in the attacker target. Dropping
    ``exist_ok`` makes the create atomic; the ``FileExistsError`` is re-``lstat``-ed
    and the raced-in symlink refused. The patched ``mkdir`` below stands in for
    that adversarial scheduler by planting the link the instant before the create.
    """
    attacker_dir = tmp_path / "attacker"
    attacker_dir.mkdir()
    attacker_dir.chmod(0o755)
    anchor = tmp_path / "home"
    anchor.mkdir()
    secrets_dir = anchor / "secrets"  # missing leaf; ``anchor`` is the real anchor
    target = secrets_dir / "github.secret"

    real_mkdir = Path.mkdir

    def racing_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        # Win the TOCTOU race: a symlink to the attacker dir appears at the secrets
        # component the instant before our create lands.
        if self == secrets_dir and not self.exists() and not self.is_symlink():
            self.symlink_to(attacker_dir)
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)

    with pytest.raises(CredentialError) as excinfo:
        credentials._write_secret_file(target, "tok-value")

    assert excinfo.value.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE
    # The secret never followed the raced-in link into the attacker target...
    assert list(attacker_dir.iterdir()) == []
    # ...and the link target was not hardened to 0o700 through the symlink.
    assert stat.S_IMODE(attacker_dir.stat().st_mode) == 0o755
    assert "tok-value" not in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="chmod hardening only runs on POSIX")
def test_write_secret_file_rejects_regular_file_racing_into_mkdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-directory racing into a missing component is refused, not chmod'd.

    Companion to the symlink-race guard: the re-``lstat`` after a concurrent
    ``FileExistsError`` must also refuse a plain file that appears in the window,
    so the hardening chmod never re-permissions an unrelated raced-in file.
    """
    anchor = tmp_path / "home"
    anchor.mkdir()
    secrets_dir = anchor / "secrets"
    target = secrets_dir / "github.secret"

    real_mkdir = Path.mkdir

    def racing_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        if self == secrets_dir and not self.exists():
            self.write_text("raced-in unrelated file")
            self.chmod(0o644)
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)

    with pytest.raises(CredentialError) as excinfo:
        credentials._write_secret_file(target, "tok-value")

    assert excinfo.value.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE
    # The raced-in file was neither re-permissioned to 0o700 nor written through.
    assert stat.S_IMODE(secrets_dir.stat().st_mode) == 0o644
    assert secrets_dir.read_text() == "raced-in unrelated file"
    assert "tok-value" not in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="symlink semantics are POSIX-specific")
def test_write_secret_file_tolerates_real_dir_racing_into_mkdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real directory built concurrently is tolerated (mkdir -p idempotency).

    Dropping ``exist_ok`` must not turn a benign concurrent create of the same
    managed tree — by a parallel trusted host-setup run — into a hard failure: a
    plain *directory* that races in is accepted and the secret is still written.
    """
    anchor = tmp_path / "home"
    anchor.mkdir()
    secrets_dir = anchor / "secrets"
    target = secrets_dir / "github.secret"

    real_mkdir = Path.mkdir

    def racing_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        # A trusted concurrent run materialises the real directory first.
        if self == secrets_dir and not self.exists():
            real_mkdir(self, mode=0o700)
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)

    credentials._write_secret_file(target, "tok-value")

    assert target.read_text() == "tok-value"
    assert stat.S_IMODE(secrets_dir.stat().st_mode) == 0o700


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="symlink semantics are POSIX-specific")
def test_plain_file_backend_refuses_symlinked_secrets_dir(tmp_path: Path) -> None:
    """``create_ref`` refuses a symlinked secrets dir end-to-end (no resolve-away).

    The backend must normalise the configured secrets dir *without* following
    symlinks: a ``resolve()`` would collapse ``secrets -> attacker`` into the
    target before the write-time symlink guard could see it, leaking the secret
    into the attacker-controlled directory. With non-following normalisation the
    guard fires and the secret never reaches the link target.
    """
    attacker_dir = tmp_path / "attacker"
    attacker_dir.mkdir()
    attacker_dir.chmod(0o755)
    secrets_link = tmp_path / "secrets"
    secrets_link.symlink_to(attacker_dir)
    backend = PlainFileCredentialBackend(
        capabilities=_HEADLESS_LINUX,
        allow_plain_secrets=True,
        consent=True,
        secrets_dir=secrets_link,
    )

    with pytest.raises(CredentialError) as excinfo:
        backend.create_ref(CredentialRequest(provider="openai", secret_source=_secret(_FAKE_TOKEN)))

    assert excinfo.value.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE
    assert list(attacker_dir.iterdir()) == []
    assert stat.S_IMODE(attacker_dir.stat().st_mode) == 0o755
    assert _FAKE_TOKEN not in str(excinfo.value.to_dict())
