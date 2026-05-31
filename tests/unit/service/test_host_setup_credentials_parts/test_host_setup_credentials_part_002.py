"""CredentialRef validation, optional keyring import, and storage-cap tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

import awf.host_setup as host_setup
import awf.host_setup.credentials as credentials
from awf.host_setup.config import ProviderConfig
from awf.host_setup.credentials import (
    CREDENTIAL_BACKEND_UNAVAILABLE,
    CREDENTIAL_BACKENDS,
    CREDENTIAL_REF_INVALID,
    MAX_CREDENTIAL_REF_LENGTH,
    CredentialError,
    CredentialRef,
    CredentialRequest,
    EnvRefCredentialBackend,
    KeyringCredentialBackend,
    PlainFileCredentialBackend,
    detect_host_credential_capabilities,
    select_credential_backend,
    store_provider_credential,
)
from tests.unit.service.test_host_setup_credentials_parts._helpers import (
    _FAKE_GH_TOKEN,
    _FAKE_TOKEN,
    _HEADLESS_LINUX,
    FakeKeyringModule,
    _ChainerBackend,
    _FakeKeyringErrors,
    _secret,
)


# --------------------------------------------------------------------------- #
# CredentialRef validation and selection edge cases.
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_credential_ref_rejects_mismatched_prefix() -> None:
    """Verify a ref must use the scheme matching its backend kind."""
    with pytest.raises(ValidationError):
        CredentialRef(backend="keyring", ref="env://GH_TOKEN")


@pytest.mark.unit
def test_credential_ref_rejects_secret_like_value() -> None:
    """Verify a ref that resembles a raw secret is rejected and redacted."""
    with pytest.raises(ValidationError) as exc_info:
        CredentialRef(backend="env_ref", ref=f"env://{_FAKE_GH_TOKEN}")

    assert _FAKE_GH_TOKEN not in str(exc_info.value)


@pytest.mark.unit
@pytest.mark.parametrize("name", ["GITHUB_PAT_TOKEN", "GHP_TOKEN", "GHU_TOKEN"])
def test_credential_ref_allows_descriptive_env_var_name(name: str) -> None:
    """Verify a valid uppercase env-var name is not flagged as a raw secret.

    The raw-secret guard in ``_validate_ref`` rejects an embedded token *value*,
    but a legitimate uppercase identifier such as ``GITHUB_PAT_TOKEN``/``GHP_TOKEN``
    only collides with the lowercase provider prefixes when case is folded. An
    ``env://NAME`` ref carries a variable name, not a value, so it must construct
    cleanly while a real (lowercase-prefixed) token stays rejected.
    """
    ref = CredentialRef(backend="env_ref", ref=f"env://{name}")
    assert ref.ref == f"env://{name}"


@pytest.mark.unit
def test_credential_ref_validation_error_does_not_echo_input() -> None:
    """Verify Pydantic never echoes the offending input into the error string.

    Without ``hide_input_in_errors=True`` Pydantic appends an ``input_value=``
    clause whose (truncated) repr leaks a recognizable suffix of the raw secret
    into ``str(exc)`` — the value ``logger.exception`` formats. Guard against
    that regression for any secret-bearing ref.
    """
    secret = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0PoNmLkJiHgFeDcBa"
    with pytest.raises(ValidationError) as exc_info:
        CredentialRef(backend="env_ref", ref=f"env://{secret}")

    rendered = str(exc_info.value)
    assert secret not in rendered
    # The distinctive tail must not survive Pydantic's middle truncation either.
    assert secret[-20:] not in rendered
    assert "input_value" not in rendered


@pytest.mark.unit
def test_select_credential_backend_rejects_unknown_preference() -> None:
    """Verify an unknown backend preference is reason-coded, listing valid kinds."""
    with pytest.raises(CredentialError) as exc_info:
        select_credential_backend(
            preferred="hsm",
            capabilities=_HEADLESS_LINUX,
            allow_plain_secrets=False,
            plain_file_consent=False,
        )

    error = exc_info.value
    assert error.reason_code == CREDENTIAL_REF_INVALID
    assert error.details["valid_backends"] == list(CREDENTIAL_BACKENDS)


@pytest.mark.unit
def test_select_plain_file_returns_gated_backend() -> None:
    """Verify selecting plain_file returns the gated plain-file backend."""
    selected = select_credential_backend(
        preferred="plain_file",
        capabilities=_HEADLESS_LINUX,
        allow_plain_secrets=True,
        plain_file_consent=True,
    )
    assert selected.kind == "plain_file"


@pytest.mark.unit
@pytest.mark.parametrize("provider", ["../evil", "bad/provider", ""])
def test_keyring_rejects_unsafe_provider_identifier(provider: str) -> None:
    """Verify unsafe provider identifiers are rejected before any secret use."""
    backend = KeyringCredentialBackend(keyring_module=FakeKeyringModule())
    with pytest.raises(CredentialError) as exc_info:
        backend.create_ref(
            CredentialRequest(provider=provider, secret_source=_secret(_FAKE_GH_TOKEN))
        )

    assert exc_info.value.reason_code == CREDENTIAL_REF_INVALID


@pytest.mark.unit
@pytest.mark.parametrize(
    ("provider", "account", "field"),
    [
        (_FAKE_TOKEN, "default", "provider"),
        ("github", _FAKE_GH_TOKEN, "account"),
    ],
)
def test_keyring_rejects_token_shaped_identifier(
    provider: str,
    account: str,
    field: str,
) -> None:
    """Verify token-shaped provider/account identifiers are refused pre-storage.

    A token accidentally populating ``provider``/``account`` still matches the
    filename-safe regex, so without an explicit token-shape guard it would be
    interpolated into the keychain service/account name before the resulting ref
    is rejected. The guard must refuse it with the same secret-free reason code
    env var names already use, and never reach the keychain write.
    """
    module = FakeKeyringModule()
    backend = KeyringCredentialBackend(keyring_module=module)
    with pytest.raises(CredentialError) as exc_info:
        backend.create_ref(
            CredentialRequest(
                provider=provider,
                account=account,
                secret_source=_secret(_FAKE_GH_TOKEN),
            )
        )

    error = exc_info.value
    assert error.reason_code == CREDENTIAL_REF_INVALID
    assert error.details == {"field": field}
    assert module.set_calls == []
    assert _FAKE_TOKEN not in str(error.to_dict())
    assert _FAKE_GH_TOKEN not in str(error.to_dict())


@pytest.mark.unit
def test_plain_file_rejects_token_shaped_provider(tmp_path: Path) -> None:
    """Verify a token-shaped provider is refused before any plain-file write.

    The provider is interpolated into the secret file path, so a token-shaped
    value must be rejected with a secret-free reason code and leave neither the
    secret file nor the secrets directory behind.
    """
    secrets_dir = tmp_path / "secrets"
    backend = PlainFileCredentialBackend(
        capabilities=_HEADLESS_LINUX,
        allow_plain_secrets=True,
        consent=True,
        secrets_dir=secrets_dir,
    )
    with pytest.raises(CredentialError) as exc_info:
        backend.create_ref(
            CredentialRequest(provider=_FAKE_TOKEN, secret_source=_secret(_FAKE_TOKEN))
        )

    error = exc_info.value
    assert error.reason_code == CREDENTIAL_REF_INVALID
    assert error.details == {"field": "provider"}
    assert not secrets_dir.exists()
    assert _FAKE_TOKEN not in str(error.to_dict())


@pytest.mark.unit
def test_keyring_set_password_failure_is_reason_coded() -> None:
    """Verify keychain write failures surface as backend-unavailable errors."""
    backend = KeyringCredentialBackend(keyring_module=FakeKeyringModule(raise_on_set=True))
    with pytest.raises(CredentialError) as exc_info:
        backend.create_ref(
            CredentialRequest(provider="github", secret_source=_secret(_FAKE_GH_TOKEN))
        )

    error = exc_info.value
    assert error.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE
    assert _FAKE_GH_TOKEN not in str(error.to_dict())


@pytest.mark.unit
@pytest.mark.parametrize(
    "set_error",
    [
        OSError("DBus session unavailable"),
        RuntimeError("partially-configured keychain stack"),
    ],
)
def test_keyring_non_keyring_error_is_reason_coded(set_error: BaseException) -> None:
    """Verify non-``KeyringError`` write failures are translated, not propagated.

    Real keyring backends can raise standard exceptions that do not inherit from
    ``KeyringError`` (``OSError`` when DBus is unavailable, a bare ``RuntimeError``
    from a partially-configured stack). These must surface as a reason-coded
    ``CredentialError`` with the original type preserved, never crash the caller,
    and never leak the secret.
    """
    backend = KeyringCredentialBackend(keyring_module=FakeKeyringModule(set_error=set_error))
    with pytest.raises(CredentialError) as exc_info:
        backend.create_ref(
            CredentialRequest(provider="github", secret_source=_secret(_FAKE_GH_TOKEN))
        )

    error = exc_info.value
    assert error.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE
    assert error.details == {"backend": "keyring", "error_type": type(set_error).__name__}
    assert error.__cause__ is set_error
    assert _FAKE_GH_TOKEN not in str(error.to_dict())


@pytest.mark.unit
def test_keyring_create_ref_rejects_backend_that_drops_write() -> None:
    """Verify a non-noop backend that silently drops the write yields no ref.

    On headless Linux ``keyring.get_keyring()`` can resolve to a ChainerBackend
    with no usable child: its module leaf is not ``fail``/``null`` so it passes
    the availability probe, and its ``set_password`` returns without raising while
    storing nothing. Without a read-back check ``create_ref`` would mint a
    ``keyring://`` reference pointing at an empty slot that silently fails at
    resolution time, so the write is verified and degrades to a reason-coded
    unavailable error instead of a bogus ref.
    """
    module = FakeKeyringModule(backend=_ChainerBackend(), drop_writes=True)
    backend = KeyringCredentialBackend(keyring_module=module)
    # The chainer backend is not a fail/null no-op, so availability reports True.
    assert backend.is_available() is True

    with pytest.raises(CredentialError) as exc_info:
        backend.create_ref(
            CredentialRequest(provider="github", secret_source=_secret(_FAKE_GH_TOKEN))
        )

    error = exc_info.value
    assert error.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE
    assert error.details == {"backend": "keyring"}
    # The write was attempted, but the empty read-back blocks the bogus ref and
    # the secret never leaks into the error surface.
    assert module.set_calls == [("awf/github", "default", _FAKE_GH_TOKEN)]
    assert _FAKE_GH_TOKEN not in str(error.to_dict())


@pytest.mark.unit
def test_keyring_create_ref_readback_failure_is_reason_coded() -> None:
    """Verify a read-back error after the write maps to a secret-free reason code.

    The read-back touches the same unbounded third-party surface as the write, so
    a ``get_password`` that raises (e.g. ``OSError`` when DBus drops mid-call)
    must surface as a reason-coded ``CredentialError`` with the original type
    preserved, never crash the caller, and never leak the secret.
    """
    read_error = OSError("DBus session closed")
    module = FakeKeyringModule(read_back_error=read_error)
    backend = KeyringCredentialBackend(keyring_module=module)

    with pytest.raises(CredentialError) as exc_info:
        backend.create_ref(
            CredentialRequest(provider="github", secret_source=_secret(_FAKE_GH_TOKEN))
        )

    error = exc_info.value
    assert error.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE
    assert error.details == {"backend": "keyring", "error_type": "OSError"}
    assert error.__cause__ is read_error
    assert "DBus session closed" not in str(error.to_dict())
    assert _FAKE_GH_TOKEN not in str(error.to_dict())


@pytest.mark.unit
def test_keyring_create_ref_discards_orphaned_write_on_readback_failure() -> None:
    """Verify a persisted-but-unverified write is deleted before signalling unavailable.

    ``set_password`` succeeds (the secret reaches the keychain), then the read-back
    ``get_password`` raises — durability cannot be confirmed, so ``create_ref``
    raises ``CREDENTIAL_BACKEND_UNAVAILABLE`` and the caller degrades to env_ref.
    The just-written secret must be deleted first so the recorded ``env://`` ref
    never diverges from a value left orphaned in the OS keychain under a backend
    nothing will resolve through.
    """
    module = FakeKeyringModule(read_back_error=OSError("DBus session closed"))
    backend = KeyringCredentialBackend(keyring_module=module)

    with pytest.raises(CredentialError) as exc_info:
        backend.create_ref(
            CredentialRequest(provider="github", secret_source=_secret(_FAKE_GH_TOKEN))
        )

    assert exc_info.value.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE
    # The write was attempted, then cleaned up: no secret is orphaned in the keychain.
    assert module.set_calls == [("awf/github", "default", _FAKE_GH_TOKEN)]
    assert module.delete_calls == [("awf/github", "default")]
    assert module._stored == {}


@pytest.mark.unit
def test_store_cleans_up_orphaned_keyring_write_before_env_ref_fallback() -> None:
    """Verify degrading to env_ref never leaves the keyring write orphaned.

    A read-back failure after a persisting ``set_password`` makes ``create_ref``
    raise ``CREDENTIAL_BACKEND_UNAVAILABLE``, so ``store_provider_credential``
    degrades to the ``env://`` offer. Recording ``env://NAME`` while the secret
    still lives in the OS keychain would point the host config at the wrong
    backend, so the orphaned write must be removed before the fallback runs.
    """
    module = FakeKeyringModule(read_back_error=OSError("DBus session closed"))
    keyring_backend = KeyringCredentialBackend(keyring_module=module)

    ref = store_provider_credential(
        CredentialRequest(
            provider="github",
            env_var="GH_TOKEN",
            secret_source=_secret(_FAKE_GH_TOKEN),
        ),
        preferred=None,
        capabilities=_HEADLESS_LINUX,
        allow_plain_secrets=False,
        plain_file_consent=False,
        keyring_backend=keyring_backend,
    )

    assert ref.backend == "env_ref"
    assert ref.ref == "env://GH_TOKEN"
    assert module.set_calls == [("awf/github", "default", _FAKE_GH_TOKEN)]
    assert module.delete_calls == [("awf/github", "default")]
    assert module._stored == {}


@pytest.mark.unit
def test_keyring_unavailable_module_is_reason_coded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify a missing keyring module reports backend unavailable on use.

    The lazy import is forced to ``None`` so the test is hermetic and never
    touches a real OS keychain even when ``keyring`` is installed.
    """
    monkeypatch.setattr(credentials, "_import_keyring_module", lambda: None)
    backend = KeyringCredentialBackend(keyring_module=None)
    assert backend.is_available() is False
    with pytest.raises(CredentialError) as exc_info:
        backend.create_ref(
            CredentialRequest(provider="github", secret_source=_secret(_FAKE_GH_TOKEN))
        )

    assert exc_info.value.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE


# --------------------------------------------------------------------------- #
# Optional keyring import behaviour.
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_import_keyring_module_returns_module_when_importable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the optional keyring import returns the module when present."""
    sentinel = FakeKeyringModule()

    def _fake_import(name: str) -> object:
        """Return the fake module for the keyring import only."""
        assert name == "keyring"
        return sentinel

    monkeypatch.setattr(credentials.importlib, "import_module", _fake_import)
    assert credentials._import_keyring_module() is sentinel


@pytest.mark.unit
def test_import_keyring_module_returns_none_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the optional keyring import degrades to ``None`` when absent."""

    def _missing_import(name: str) -> object:
        """Raise ``ImportError`` as an absent keyring install would."""
        raise ImportError("No module named 'keyring'")

    monkeypatch.setattr(credentials.importlib, "import_module", _missing_import)
    assert credentials._import_keyring_module() is None


@pytest.mark.unit
def test_keyring_backend_uses_lazy_import_when_module_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the keyring backend consults the lazy import when not injected."""
    monkeypatch.setattr(credentials, "_import_keyring_module", lambda: None)
    assert KeyringCredentialBackend().is_available() is False

    monkeypatch.setattr(credentials, "_import_keyring_module", lambda: FakeKeyringModule())
    assert KeyringCredentialBackend().is_available() is True


@pytest.mark.unit
def test_env_ref_backend_is_always_available() -> None:
    """Verify the env-ref backend needs no storage and is always available."""
    assert EnvRefCredentialBackend().is_available() is True


@pytest.mark.unit
def test_keyring_unavailable_when_module_lacks_get_keyring() -> None:
    """Verify a module without ``get_keyring`` is treated as unavailable."""

    class _NoGetKeyringModule:
        """Keyring-like module missing the ``get_keyring`` entry point."""

        errors = _FakeKeyringErrors

        def set_password(self, service: str, username: str, password: str) -> None:
            """Record nothing; this fake is never reached for writes."""

    backend = KeyringCredentialBackend(keyring_module=_NoGetKeyringModule())
    assert backend.is_available() is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "get_error",
    [
        OSError("DBus session unavailable"),
        RuntimeError("partially-configured keychain stack"),
    ],
)
def test_keyring_availability_swallows_non_keyring_get_errors(
    get_error: BaseException,
) -> None:
    """Verify a non-``KeyringError`` from ``get_keyring`` degrades to unavailable.

    On headless Linux a partially-configured D-Bus stack can make
    ``get_keyring()`` raise ``OSError``/``RuntimeError`` (or a ``DBusException``)
    rather than a ``KeyringError`` subclass. Availability detection must treat
    these as "no usable backend" so credential selection falls back to env-ref,
    never propagate and crash on exactly the hosts this fallback is meant to serve.
    """
    keyring_backend = KeyringCredentialBackend(
        keyring_module=FakeKeyringModule(get_error=get_error)
    )
    assert keyring_backend.is_available() is False

    selected = select_credential_backend(
        preferred=None,
        capabilities=_HEADLESS_LINUX,
        allow_plain_secrets=False,
        plain_file_consent=False,
        keyring_backend=keyring_backend,
    )
    assert selected.kind == "env_ref"


@pytest.mark.unit
def test_keyring_unavailable_when_backend_resolves_to_none() -> None:
    """Verify a keyring module resolving to no backend is unavailable."""

    class _NullBackendModule:
        """Keyring-like module whose ``get_keyring`` returns ``None``."""

        errors = _FakeKeyringErrors

        def get_keyring(self) -> object | None:
            """Return ``None`` to model an unresolved keyring backend."""
            return None

        def set_password(self, service: str, username: str, password: str) -> None:
            """Record nothing; this fake is never reached for writes."""

    backend = KeyringCredentialBackend(keyring_module=_NullBackendModule())
    assert backend.is_available() is False


@pytest.mark.unit
def test_third_party_backend_with_noop_named_module_stays_available() -> None:
    """Verify a non-keyring backend whose module leaf is ``null``/``fail`` is usable.

    The no-op probe must match only the ``fail``/``null`` backends ``keyring``
    itself resolves to. A fully functional third-party backend from e.g.
    ``myvault.null`` shares that leaf name but is not a keyring no-op, so scoping
    the check to ``keyring``-rooted modules keeps it reporting available rather
    than being silently rejected and degraded to env-ref.
    """

    class _ThirdPartyVaultBackend:
        """Functional third-party backend whose module leaf collides with ``null``."""

    _ThirdPartyVaultBackend.__module__ = "myvault.null"

    backend = KeyringCredentialBackend(
        keyring_module=FakeKeyringModule(backend=_ThirdPartyVaultBackend())
    )
    assert backend.is_available() is True


@pytest.mark.unit
def test_plain_file_dir_creation_failure_is_reason_coded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify a secrets-dir creation failure is a secret-free reason code."""

    def _mkdir_fails(self: Path, *args: object, **kwargs: object) -> None:
        """Raise an `OSError` before any temp secret file is created."""
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "mkdir", _mkdir_fails)
    backend = PlainFileCredentialBackend(
        capabilities=_HEADLESS_LINUX,
        allow_plain_secrets=True,
        consent=True,
        secrets_dir=tmp_path / "secrets",
    )
    with pytest.raises(CredentialError) as exc_info:
        backend.create_ref(CredentialRequest(provider="openai", secret_source=_secret(_FAKE_TOKEN)))

    error = exc_info.value
    assert error.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE
    assert error.details == {"error_type": "OSError"}
    assert _FAKE_TOKEN not in str(error.to_dict())


@pytest.mark.unit
def test_chmod_best_effort_skips_non_posix_hosts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify the chmod helper is a no-op on non-POSIX hosts."""
    target = tmp_path / "secret"
    target.write_text("x", encoding="utf-8")
    monkeypatch.setattr(credentials.os, "name", "nt")

    credentials._chmod_best_effort(target, 0o600)


@pytest.mark.unit
def test_fsync_dir_best_effort_skips_non_posix_hosts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify the directory-fsync helper is a no-op on non-POSIX hosts.

    Directory fsync is POSIX-only — ``os.open`` on a directory fails on Windows —
    so the helper must return before touching the filesystem off POSIX rather
    than raising and tearing down a write whose data is already durable.
    """
    monkeypatch.setattr(credentials.os, "name", "nt")

    def _fail_open(*args: object, **kwargs: object) -> int:
        raise AssertionError("os.open must not be called on non-POSIX hosts")

    def _fail_fsync(fd: int) -> None:
        raise AssertionError("os.fsync must not be called on non-POSIX hosts")

    monkeypatch.setattr(credentials.os, "open", _fail_open)
    monkeypatch.setattr(credentials.os, "fsync", _fail_fsync)

    credentials._fsync_dir_best_effort(tmp_path)


@pytest.mark.unit
def test_reject_group_or_world_accessible_dir_skips_non_posix_hosts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify the owner-private directory guard is a no-op on non-POSIX hosts.

    The rwx-for-group/other permission model the guard enforces is POSIX-specific
    (``_chmod_best_effort`` is itself a no-op off POSIX), so the guard must return
    without refusing a directory whose mode carries group/other bits the platform
    ignores — a 0o755 dir that would be fatal on POSIX is accepted off it.
    """
    target = tmp_path / "secrets"
    target.mkdir()
    target.chmod(0o755)  # group/world-accessible: fatal on POSIX, ignored off it
    monkeypatch.setattr(credentials.os, "name", "nt")

    credentials._reject_group_or_world_accessible_dir(target)


@pytest.mark.unit
def test_reject_writable_ancestor_skips_non_posix_hosts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify the writable-ancestor swap-vector guard is a no-op on non-POSIX hosts.

    The rename-permission semantics the guard relies on (sticky-bit ownership,
    group/other write bits) are POSIX-specific — ``_chmod_best_effort`` is itself a
    no-op off POSIX — so the guard must return without refusing an ancestor whose
    mode carries group/other write bits the platform ignores: a 0o777 dir that
    would be a fatal swap vector on POSIX is accepted off it.
    """
    ancestor = tmp_path / "shared"
    ancestor.mkdir()
    ancestor.chmod(0o777)  # group/world-writable, non-sticky: fatal on POSIX, ignored off it
    monkeypatch.setattr(credentials.os, "name", "nt")

    credentials._reject_writable_ancestor(ancestor)


def _stat_owned_by(ancestor: Path, uid: int) -> object:
    """Return a ``Path.stat`` replacement reporting ``ancestor`` as owned by ``uid``.

    The actual ``tmp_path`` tree is owned by the test process, so to model an
    ancestor owned by *another* user (or by root) without privileges we rewrite
    only the ``st_uid`` field of that one directory's stat result, leaving every
    other path's stat untouched. ``list(stat_result)`` yields the ten basic
    sequence fields with ``st_uid`` at index 4, which ``os.stat_result`` rebuilds.
    """
    real_stat = Path.stat

    def fake_stat(self: Path, *args: object, **kwargs: object) -> os.stat_result:
        info = real_stat(self, *args, **kwargs)
        if self == ancestor:
            fields = list(info)
            fields[4] = uid  # st_uid
            return os.stat_result(fields)
        return info

    return fake_stat


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="sticky-bit ownership semantics are POSIX-specific")
def test_reject_writable_ancestor_refuses_sticky_dir_owned_by_other_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A sticky world-writable ancestor owned by *another* local user is still refused.

    Regression for PRRT_kwDOSJAM6s6F8zey: the sticky-bit exemption assumes only a
    *non-owner* attacker. On a sticky directory rename/delete of an entry is
    restricted to the entry's owner, the *directory's* owner, and root — so when the
    sticky ancestor itself is owned by another local user (e.g. an operator- or
    attacker-provided ``/tmp/attacker`` at ``0o1777``), that owner can still rename
    AWF's secrets dir aside and plant a redirecting symlink before the later secret
    write, defeating the symlink checks. The exemption must verify the ancestor is
    root- or current-user-owned first; here it is reported owned by a synthetic third
    user (neither root nor the current euid), so the default ``allow_sticky=True``
    must not save it.
    """
    ancestor = tmp_path / "attacker"
    ancestor.mkdir()
    ancestor.chmod(0o1777)  # world-writable WITH the sticky bit, like /tmp
    other_uid = os.geteuid() + 1  # neither root (0) nor the current effective user
    monkeypatch.setattr(Path, "stat", _stat_owned_by(ancestor, other_uid))

    with pytest.raises(PermissionError):
        credentials._reject_writable_ancestor(ancestor)


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="ownership/rename semantics are POSIX-specific")
def test_reject_writable_ancestor_refuses_non_writable_dir_owned_by_other_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An owner-private (0o700) ancestor owned by *another* local user is still refused.

    Regression for PRRT_kwDOSJAM6s6F83VM: clear group/other write bits do not on their
    own make a pre-existing ancestor safe. A directory's *owner* always holds rename
    rights over the entries within it (an owner of even a 0o700 dir has rwx on it), so
    under elevated privileges (AWF runs as root) the owner of an owner-private 0o700
    ancestor can still rename AWF's secrets dir aside and plant a redirecting symlink
    before the later secret write, defeating the symlink checks. The guard must require
    trusted ownership (root or the current euid), not merely non-writable mode bits;
    here the ancestor is reported owned by a synthetic third user (neither root nor the
    current euid), which the pre-fix mode-only early return wrongly treated as safe.
    """
    ancestor = tmp_path / "private"
    ancestor.mkdir()
    ancestor.chmod(0o700)  # owner-private: no group/other write bits at all
    other_uid = os.geteuid() + 1  # neither root (0) nor the current effective user
    monkeypatch.setattr(Path, "stat", _stat_owned_by(ancestor, other_uid))

    with pytest.raises(PermissionError):
        credentials._reject_writable_ancestor(ancestor)


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="sticky-bit ownership semantics are POSIX-specific")
def test_reject_writable_ancestor_exempts_root_owned_sticky_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A root-owned sticky world-writable ancestor (the real ``/tmp``) stays exempted.

    Complement to the not-owned refusal (PRRT_kwDOSJAM6s6F8zey): the ownership check
    must not over-refuse the legitimate case it exists to protect. A sticky directory
    owned by root restricts rename/delete to the entry owner (AWF) and root, so a
    non-root local user cannot swap AWF's secrets dir there. Reporting the ancestor as
    root-owned (robust whether or not the test itself runs as root) must leave the
    exemption intact — no exception.
    """
    ancestor = tmp_path / "tmp"
    ancestor.mkdir()
    ancestor.chmod(0o1777)  # world-writable WITH the sticky bit, like /tmp
    monkeypatch.setattr(Path, "stat", _stat_owned_by(ancestor, 0))

    credentials._reject_writable_ancestor(ancestor)


@pytest.mark.unit
@pytest.mark.skipif(
    os.name != "posix", reason="ownership/permission-bypass semantics are POSIX-specific"
)
def test_reject_untrusted_owner_dir_refuses_leaf_owned_by_other_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A pre-existing owner-private (0o700) leaf owned by *another* local user is refused.

    Regression for PRRT_kwDOSJAM6s6F83VO: the leaf secrets dir is accepted on its
    *mode* alone (``_reject_group_or_world_accessible_dir``), but owner-private bits do
    not pin who owns it. AWF's containers run as root, and root bypasses the permission
    bits — it will write the credential file into a 0o700 secrets dir owned by another
    local user, whose owner then controls the entries within and can delete or replace
    the ``plain-file://`` target after setup so the stored ref no longer points to
    AWF-controlled storage. The leaf must be owned by root or the current euid; here it
    is reported owned by a synthetic third user (neither root nor the current euid),
    which the pre-fix mode-only check wrongly treated as safe.
    """
    leaf = tmp_path / "secrets"
    leaf.mkdir(mode=0o700)  # owner-private: passes the mode-only leaf check
    other_uid = os.geteuid() + 1  # neither root (0) nor the current effective user
    monkeypatch.setattr(Path, "stat", _stat_owned_by(leaf, other_uid))

    with pytest.raises(PermissionError):
        credentials._reject_untrusted_owner_dir(leaf)


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="ownership semantics are POSIX-specific")
def test_reject_untrusted_owner_dir_accepts_root_owned_leaf(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A root-owned leaf secrets dir is accepted, not over-refused.

    Complement to the not-owned refusal (PRRT_kwDOSJAM6s6F83VO): the ownership check
    must not reject the legitimate case where an operator pre-creates the secrets dir
    as root. Reporting the leaf as root-owned (robust whether or not the test itself
    runs as root) must leave it accepted — no exception.
    """
    leaf = tmp_path / "secrets"
    leaf.mkdir(mode=0o700)
    monkeypatch.setattr(Path, "stat", _stat_owned_by(leaf, 0))

    credentials._reject_untrusted_owner_dir(leaf)


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="ownership semantics are POSIX-specific")
def test_reject_untrusted_owner_dir_accepts_current_user_leaf(tmp_path: Path) -> None:
    """A leaf owned by the current effective user (the common reuse case) is accepted.

    The default ``~/.awf/secrets`` and any leaf AWF created this call are owned by the
    effective user; the ownership guard must let them through untouched, so the common
    reuse path is unaffected by the not-owned refusal above.
    """
    leaf = tmp_path / "secrets"
    leaf.mkdir(mode=0o700)  # owned by the test process == current euid

    credentials._reject_untrusted_owner_dir(leaf)


@pytest.mark.unit
def test_reject_untrusted_owner_dir_skips_non_posix_hosts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify the leaf-ownership guard is a no-op on non-POSIX hosts.

    The ownership/permission-bypass model the guard enforces is POSIX-specific
    (``_chmod_best_effort`` is itself a no-op off POSIX, and ``os.geteuid`` may not
    exist), so the guard must return before consulting ``st_uid``: a leaf reported
    owned by another user — fatal on POSIX — is accepted off it.
    """
    leaf = tmp_path / "secrets"
    leaf.mkdir(mode=0o700)
    foreign_uid = os.geteuid() + 1  # fatal on POSIX, must be ignored off it
    monkeypatch.setattr(Path, "stat", _stat_owned_by(leaf, foreign_uid))
    monkeypatch.setattr(credentials.os, "name", "nt")

    credentials._reject_untrusted_owner_dir(leaf)


# --------------------------------------------------------------------------- #
# 12. Credential refs stay within the ProviderConfig storage cap.
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_credential_ref_cap_matches_provider_config_credential_ref() -> None:
    """Verify a max-length ref stores in ``ProviderConfig`` and one char over fails.

    ``CredentialRef.ref`` is the value later persisted to
    ``ProviderConfig.credential_ref``; if its cap drifts above that field's a
    backend can mint a ref that passes ``CredentialRef`` validation yet cannot be
    stored. Pin both caps to the same boundary to keep them in lockstep.
    """
    cap = MAX_CREDENTIAL_REF_LENGTH
    at_cap = "env://" + "A" * (cap - len("env://"))
    assert len(at_cap) == cap
    assert CredentialRef(backend="env_ref", ref=at_cap).ref == at_cap
    assert ProviderConfig(credential_ref=at_cap, backend="env_ref").credential_ref == at_cap

    over_cap = at_cap + "A"
    with pytest.raises(ValidationError):
        CredentialRef(backend="env_ref", ref=over_cap)
    with pytest.raises(ValidationError):
        ProviderConfig(credential_ref=over_cap, backend="env_ref")


@pytest.mark.unit
def test_plain_file_overlong_ref_fails_before_writing_secret(tmp_path: Path) -> None:
    """Verify an over-long plain-file ref is rejected before any secret write.

    A provider long enough to push ``plain-file://<dir>/<provider>`` past the
    ProviderConfig cap must fail with no secret file — and no secrets dir — left
    behind: the write must not happen before the ref is known to be storable.
    """
    secrets_dir = tmp_path / "secrets"
    long_provider = "a" * (MAX_CREDENTIAL_REF_LENGTH + 50)
    backend = PlainFileCredentialBackend(
        capabilities=_HEADLESS_LINUX,
        allow_plain_secrets=True,
        consent=True,
        secrets_dir=secrets_dir,
    )
    with pytest.raises(CredentialError) as exc_info:
        backend.create_ref(
            CredentialRequest(provider=long_provider, secret_source=_secret(_FAKE_TOKEN))
        )

    error = exc_info.value
    assert error.reason_code == CREDENTIAL_REF_INVALID
    assert not (secrets_dir / long_provider).exists()
    assert not secrets_dir.exists()
    assert _FAKE_TOKEN not in str(error.to_dict())


@pytest.mark.unit
def test_keyring_overlong_ref_fails_before_storing_secret() -> None:
    """Verify an over-long keyring ref is rejected before the keychain write."""
    module = FakeKeyringModule()
    backend = KeyringCredentialBackend(keyring_module=module)
    long_provider = "a" * (MAX_CREDENTIAL_REF_LENGTH + 50)
    with pytest.raises(CredentialError) as exc_info:
        backend.create_ref(
            CredentialRequest(provider=long_provider, secret_source=_secret(_FAKE_GH_TOKEN))
        )

    error = exc_info.value
    assert error.reason_code == CREDENTIAL_REF_INVALID
    assert module.set_calls == []
    assert _FAKE_GH_TOKEN not in str(error.to_dict())


@pytest.mark.unit
def test_env_ref_overlong_name_is_rejected() -> None:
    """Verify a valid-but-over-long env var name is rejected at the storage cap.

    The env-ref backend stores nothing, but its ref must still fit ProviderConfig;
    a POSIX-valid name long enough to overflow the cap must be refused.
    """
    long_name = "A" * (MAX_CREDENTIAL_REF_LENGTH + 50)
    with pytest.raises(CredentialError) as exc_info:
        EnvRefCredentialBackend().create_ref(
            CredentialRequest(provider="openai", env_var=long_name)
        )

    assert exc_info.value.reason_code == CREDENTIAL_REF_INVALID


@pytest.mark.unit
def test_plain_file_ref_validation_error_is_reason_coded(tmp_path: Path) -> None:
    """Verify a ref that fails ``CredentialRef`` validation raises ``CredentialError``.

    ``_build_credential_ref`` assembles the ref from a ``secrets_dir`` it does not
    sanitise, so a secrets dir whose own path component is token-shaped makes the
    assembled ``plain-file://`` ref trip ``CredentialRef``'s raw-secret guard,
    which raises a Pydantic ``ValidationError``. Every backend promises to raise
    only ``CredentialError``, so that internal validation failure must be wrapped
    with a reason code rather than escaping ``store_provider_credential`` raw —
    and the secret must never reach the keychain/file or the diagnostics.
    """
    token_shaped_dir = tmp_path / ("ghp_" + "c" * 36) / "secrets"
    backend = PlainFileCredentialBackend(
        capabilities=_HEADLESS_LINUX,
        allow_plain_secrets=True,
        consent=True,
        secrets_dir=token_shaped_dir,
    )
    with pytest.raises(CredentialError) as exc_info:
        backend.create_ref(
            CredentialRequest(provider="github", secret_source=_secret(_FAKE_GH_TOKEN))
        )

    error = exc_info.value
    assert error.reason_code == CREDENTIAL_REF_INVALID
    assert not token_shaped_dir.exists()
    assert _FAKE_GH_TOKEN not in str(error.to_dict())


@pytest.mark.unit
def test_host_setup_reexports_credential_symbols() -> None:
    """Verify host setup re-exports the public credential surface additively."""
    assert host_setup.CredentialError is CredentialError
    assert host_setup.CredentialRef is CredentialRef
    assert host_setup.store_provider_credential is store_provider_credential
    assert host_setup.detect_host_credential_capabilities is (detect_host_credential_capabilities)
