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
@pytest.mark.skipif(os.name != "posix", reason="permission semantics are POSIX-specific")
def test_write_secret_file_rejects_writable_dir_racing_into_mkdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A group/world-writable *directory* racing into a missing component is refused.

    Regression for PRRT_kwDOSJAM6s6F8uMR: when ``secrets_dir`` is under a parent
    that does not exist during the symlink walk (e.g. ``<tmp>/awf/secrets`` with
    ``<tmp>/awf`` absent), a local user can win the race before AWF's ``mkdir`` and
    create that intermediate component as a real, group/world-writable directory
    they own. The ``FileExistsError`` branch previously accepted *any* real raced-in
    directory — only re-``lstat``-ing for a symlink/non-dir and chmod-ing
    best-effort — so it never ran the writable-ancestor guard the *pre-existing*
    ancestor path applies. AWF would then create the 0o700 secrets dir beneath that
    attacker-controlled writable ancestor, which can ``rename`` it aside and plant a
    redirecting symlink before the later secret write. A raced-in component must be
    treated like a pre-existing ancestor and refused when group/world-writable.
    """
    anchor = tmp_path / "home"
    anchor.mkdir()
    raced = anchor / "awf"  # intermediate component the attacker wins the race for
    secrets_dir = raced / "secrets"
    target = secrets_dir / "github.secret"

    real_mkdir = Path.mkdir

    def racing_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        # The attacker materialises the intermediate component as a group/world-
        # writable (non-sticky) directory the instant before AWF's create lands.
        if self == raced and not self.exists():
            real_mkdir(self)
            self.chmod(0o777)
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)

    with pytest.raises(CredentialError) as excinfo:
        credentials._write_secret_file(target, "tok-value")

    assert excinfo.value.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE
    # We fail closed at the raced-in writable component: no secrets dir, no secret,
    # and the attacker-owned ancestor was not silently tightened through.
    assert not secrets_dir.exists()
    assert not target.exists()
    assert stat.S_IMODE(raced.stat().st_mode) == 0o777
    assert "tok-value" not in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="sticky-bit semantics are POSIX-specific")
def test_write_secret_file_rejects_sticky_writable_dir_racing_into_mkdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A *sticky* group/world-writable directory racing in is still refused.

    Companion to the non-sticky raced-in guard (PRRT_kwDOSJAM6s6F8uMR): the sticky
    exemption in ``_reject_writable_ancestor`` is sound only for a *pre-existing*,
    trusted, non-attacker-owned parent such as a root-owned ``/tmp`` — there a
    non-owner attacker cannot rename AWF's entry. A component that races in mid-
    create can instead be one the attacker just made and *owns*, and a directory's
    owner may rename entries inside it regardless of the sticky bit. So an attacker
    could otherwise set ``0o1777`` to dodge the writable-ancestor check while still
    holding the swap right. The raced-in path must reject a group/world-writable
    component without honouring the sticky exemption.
    """
    anchor = tmp_path / "home"
    anchor.mkdir()
    raced = anchor / "awf"
    secrets_dir = raced / "secrets"
    target = secrets_dir / "github.secret"

    real_mkdir = Path.mkdir

    def racing_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        # Attacker sets the sticky bit (0o1777) trying to slip past the /tmp-style
        # exemption — but as the directory's owner they keep the rename/swap right.
        if self == raced and not self.exists():
            real_mkdir(self)
            self.chmod(0o1777)
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)

    with pytest.raises(CredentialError) as excinfo:
        credentials._write_secret_file(target, "tok-value")

    assert excinfo.value.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE
    assert not secrets_dir.exists()
    assert not target.exists()
    assert stat.S_IMODE(raced.stat().st_mode) == 0o1777
    assert "tok-value" not in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="chmod hardening only runs on POSIX")
def test_write_secret_file_refuses_unhardenable_world_traversable_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing secrets dir that stays world-traversable is refused.

    Regression for PRRT_kwDOSJAM6s6F8eRy: when an operator points ``secrets_dir``
    at an existing directory they do not own (e.g. ``/tmp`` or a root-owned 0777
    shared directory), the hardening chmod at the end of ``_mkdir_secure`` is
    *best-effort* — ``_chmod_best_effort`` suppresses the ``OSError`` a not-owned
    ``chmod`` raises — so the directory silently stays world-traversable. A
    freshly *created* dir is safe (the 0o700 create-time mode holds), but for this
    explicit-path fallback the backend would otherwise still write the secret and
    return a ``plain-file://`` ref inside the shared dir, bypassing the plain-file
    backend's own 0700 directory guarantee. The mode check must be fatal on POSIX
    *before* any secret is written. Neutralising ``_chmod_best_effort`` models the
    silently-failing not-owned chmod without needing a second uid.
    """
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(mode=0o700)
    secrets_dir.chmod(0o755)  # world-readable + world-traversable, not owner-private
    # Model an operator who cannot chmod the dir: the hardening chmod is a no-op,
    # leaving the 0o755 mode in place exactly as a suppressed ``OSError`` would.
    monkeypatch.setattr(credentials, "_chmod_best_effort", lambda *_a, **_k: None)
    target = secrets_dir / "github.secret"

    with pytest.raises(CredentialError) as excinfo:
        credentials._write_secret_file(target, "tok-value")

    assert excinfo.value.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE
    # The secret never landed in the world-traversable directory...
    assert not target.exists()
    assert list(secrets_dir.iterdir()) == []
    # ...the directory mode was left untouched (we failed closed, did not accept it).
    assert stat.S_IMODE(secrets_dir.stat().st_mode) == 0o755
    assert "tok-value" not in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="chmod hardening only runs on POSIX")
def test_write_secret_file_accepts_preexisting_owner_private_dir(tmp_path: Path) -> None:
    """A pre-existing 0o700 secrets dir is accepted (the fatal check is scoped).

    The world-traversable refusal must not regress the common reuse case: a
    secrets dir already created 0o700 by an earlier credential write is owner-
    private, so the re-stat after the hardening chmod sees no group/other bits and
    the secret is written normally.
    """
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(mode=0o700)
    target = secrets_dir / "github.secret"

    credentials._write_secret_file(target, "tok-value")

    assert target.read_text(encoding="utf-8") == "tok-value"
    assert stat.S_IMODE(secrets_dir.stat().st_mode) == 0o700


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="chmod hardening only runs on POSIX")
def test_write_secret_file_refuses_chmodable_preexisting_shared_dir(tmp_path: Path) -> None:
    """A pre-existing shared dir AWF *can* chmod is refused, never tightened.

    Regression for PRRT_kwDOSJAM6s6F8hlf, the complement of
    PRRT_kwDOSJAM6s6F8eRy: that earlier guard covered the case where the trailing
    hardening chmod in ``_mkdir_secure`` *fails* (the process cannot chmod a
    not-owned dir, so it silently stays world-traversable). This covers the case
    where the chmod *succeeds*: when ``secrets_dir`` points at an existing
    directory AWF does not own but can chmod — AWF run as root with
    ``secrets_dir=/tmp``, or an operator pointing at an owned shared workspace dir
    — the trailing chmod tightens that unrelated directory to 0o700, breaking other
    users/services that rely on the shared location and silently repurposing it as
    an AWF-private secret store, after which the group/world-accessible check
    (now passing) lets the secret land there. ``_mkdir_secure`` must only harden
    levels it created this call; a pre-existing leaf is left untouched and accepted
    only if it is *already* owner-private, so a chmodable shared dir fails closed
    without being mutated. Unlike the unhardenable-dir regression this does *not*
    neutralise ``_chmod_best_effort`` — the tmp dir is owned by the test process, so
    a real chmod would succeed, which is exactly the not-owned-but-chmodable case.
    """
    secrets_dir = tmp_path / "shared"
    secrets_dir.mkdir(mode=0o700)
    secrets_dir.chmod(0o755)  # world-traversable shared dir the process *can* chmod
    target = secrets_dir / "github.secret"

    with pytest.raises(CredentialError) as excinfo:
        credentials._write_secret_file(target, "tok-value")

    assert excinfo.value.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE
    # The shared directory was neither tightened to 0o700 nor written into.
    assert stat.S_IMODE(secrets_dir.stat().st_mode) == 0o755
    assert list(secrets_dir.iterdir()) == []
    assert not target.exists()
    assert "tok-value" not in str(excinfo.value)


def _stat_owned_by(target: Path, uid: int) -> object:
    """Return a ``Path.stat`` replacement reporting ``target`` as owned by ``uid``.

    The ``tmp_path`` tree is owned by the test process, so to model a secrets dir
    owned by *another* user without privileges we rewrite only the ``st_uid`` field of
    that one directory's stat result, leaving every other path's stat untouched.
    ``list(stat_result)`` yields the ten basic sequence fields with ``st_uid`` at
    index 4, which ``os.stat_result`` rebuilds.
    """
    real_stat = Path.stat

    def fake_stat(self: Path, *args: object, **kwargs: object) -> os.stat_result:
        info = real_stat(self, *args, **kwargs)
        if self == target:
            fields = list(info)
            fields[4] = uid  # st_uid
            return os.stat_result(fields)
        return info

    return fake_stat


@pytest.mark.unit
@pytest.mark.skipif(
    os.name != "posix", reason="ownership/permission-bypass semantics are POSIX-specific"
)
def test_write_secret_file_refuses_preexisting_leaf_owned_by_other_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing 0o700 secrets dir owned by *another* local user is refused end-to-end.

    Regression for PRRT_kwDOSJAM6s6F83VO: when ``secrets_dir`` points at an existing
    owner-private (0o700) directory owned by another local user and AWF runs elevated
    (root), the mode-only leaf check accepts it and root — bypassing the permission
    bits — writes the credential file inside. That owner controls the entries within
    their own directory, so after setup they can delete or replace the ``plain-file://``
    target with a file or symlink they control, and the stored credential ref no longer
    points to AWF-controlled secret storage. ``_mkdir_secure`` must reject a leaf owned
    by anyone but root or the effective user *before* any secret is written, leaving the
    unrelated directory untouched. The synthetic ``st_uid`` rewrite models the foreign
    owner without needing a second real uid.
    """
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(mode=0o700)  # owner-private: passes the mode-only leaf check
    other_uid = os.geteuid() + 1  # neither root (0) nor the current effective user
    monkeypatch.setattr(Path, "stat", _stat_owned_by(secrets_dir, other_uid))
    target = secrets_dir / "github.secret"

    with pytest.raises(CredentialError) as excinfo:
        credentials._write_secret_file(target, "tok-value")

    assert excinfo.value.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE
    # We fail closed before the write: no secret in the foreign-owned dir, left untouched.
    assert not target.exists()
    assert list(secrets_dir.iterdir()) == []
    assert stat.S_IMODE(secrets_dir.stat().st_mode) == 0o700
    assert "tok-value" not in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="symlink semantics are POSIX-specific")
def test_write_secret_file_refuses_writable_ancestor_of_secrets_dir(tmp_path: Path) -> None:
    """A group/world-writable, non-sticky *ancestor* of the secrets dir is refused.

    Regression for PRRT_kwDOSJAM6s6F8mAa: ``_mkdir_secure`` validates symlinks and
    hardens/checks the 0o700 leaf, then returns — but the secret is written by a
    *later* ``os.open(tmp_path)`` in ``_write_secret_file``. When the configured
    secrets dir sits under an existing group/world-writable parent, a local user
    with write access to that parent can, in that window, ``rename`` the checked
    secrets dir aside and drop a ``secrets -> /attacker`` symlink in its place; the
    temp-file open then follows the replacement into the attacker tree, exposing
    the plain-file secret. The file-level ``O_EXCL`` only guards the temp *inode*,
    not a swapped parent dir. ``_mkdir_secure`` must reject the writable ancestor
    up front — before creating anything — so the swap vector never exists.
    """
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)  # group/world-writable, NON-sticky: a rename/swap vector
    secrets_dir = shared / "secrets"  # AWF would otherwise create this 0o700
    target = secrets_dir / "github.secret"

    with pytest.raises(CredentialError) as excinfo:
        credentials._write_secret_file(target, "tok-value")

    assert excinfo.value.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE
    # We fail closed *before* the create loop: no secrets dir, no secret, and the
    # writable parent is left exactly as configured (never mutated).
    assert not secrets_dir.exists()
    assert not target.exists()
    assert stat.S_IMODE(shared.stat().st_mode) == 0o777
    assert list(shared.iterdir()) == []
    assert "tok-value" not in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="symlink semantics are POSIX-specific")
def test_write_secret_file_refuses_writable_grandparent_of_secrets_dir(tmp_path: Path) -> None:
    """A writable, non-sticky ancestor *above* the immediate parent is also refused.

    The swap vector is any writable ancestor, not just the immediate parent: a
    writable grandparent lets an attacker rename the (AWF-created, 0o700) parent
    aside and redirect it, so the symlink-refusal walk must reject a writable
    ancestor anywhere up the chain to the filesystem root — even when the levels
    below it are ones AWF creates 0o700 this call.
    """
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)  # writable grandparent above two AWF-created levels
    secrets_dir = shared / "awf" / "secrets"  # both levels would be AWF-created 0o700
    target = secrets_dir / "github.secret"

    with pytest.raises(CredentialError) as excinfo:
        credentials._write_secret_file(target, "tok-value")

    assert excinfo.value.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE
    assert not secrets_dir.exists()
    assert not (shared / "awf").exists()
    assert list(shared.iterdir()) == []
    assert "tok-value" not in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="sticky-bit semantics are POSIX-specific")
def test_write_secret_file_accepts_sticky_world_writable_ancestor(tmp_path: Path) -> None:
    """A *sticky* world-writable ancestor (e.g. ``/tmp``) is accepted, not over-refused.

    The writable-ancestor refusal must not reject the legitimate ephemeral-path
    case: on a sticky directory only an entry's *owner* may rename or delete it, so
    a non-owner attacker cannot swap the secrets dir AWF created and owns there.
    The secret is written normally — pinning that the guard exempts the sticky bit
    rather than failing closed on every world-writable ancestor (which would break
    secrets under ``/tmp`` and, with it, the test suite's own ``tmp_path`` tree).
    """
    sticky = tmp_path / "sticky"
    sticky.mkdir()
    sticky.chmod(0o1777)  # world-writable WITH the sticky bit, like /tmp
    secrets_dir = sticky / "secrets"
    target = secrets_dir / "github.secret"

    credentials._write_secret_file(target, "tok-value")

    assert target.read_text(encoding="utf-8") == "tok-value"
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
