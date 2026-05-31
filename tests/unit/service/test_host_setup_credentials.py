"""Backend selection, env-ref fallback, and plain-file storage tests."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import awf.host_setup.credentials as credentials
from awf.host_setup.config import ProviderConfig
from awf.host_setup.credentials import (
    CREDENTIAL_BACKEND_UNAVAILABLE,
    CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED,
    CREDENTIAL_REF_INVALID,
    INTERACTIVE_INPUT_REQUIRED,
    CredentialError,
    CredentialRef,
    CredentialRequest,
    EnvRefCredentialBackend,
    HostCredentialCapabilities,
    KeyringCredentialBackend,
    PlainFileCredentialBackend,
    select_credential_backend,
    store_provider_credential,
)
from tests.unit.service.test_host_setup_credentials_parts._helpers import (
    _DESKTOP_LINUX,
    _FAKE_GH_TOKEN,
    _FAKE_TOKEN,
    _HEADLESS_LINUX,
    _MACOS,
    _WINDOWS,
    FakeKeyringModule,
    _ChainerBackend,
    _FailBackend,
    _secret,
)


# --------------------------------------------------------------------------- #
# 1. Keyring is the default backend when available.
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_keyring_is_default_backend_when_available() -> None:
    """Verify auto selection prefers an available keyring backend."""
    module = FakeKeyringModule()
    keyring_backend = KeyringCredentialBackend(keyring_module=module)
    assert keyring_backend.is_available() is True

    selected = select_credential_backend(
        preferred=None,
        capabilities=_HEADLESS_LINUX,
        allow_plain_secrets=False,
        plain_file_consent=False,
        keyring_backend=keyring_backend,
    )
    assert selected.kind == "keyring"

    ref = store_provider_credential(
        CredentialRequest(provider="github", secret_source=_secret(_FAKE_GH_TOKEN)),
        preferred=None,
        capabilities=_HEADLESS_LINUX,
        allow_plain_secrets=False,
        plain_file_consent=False,
        keyring_backend=keyring_backend,
    )
    assert ref.backend == "keyring"
    assert ref.ref == "keyring://awf/github/default"
    # The secret reaches the keychain store, never the returned reference.
    assert module.set_calls == [("awf/github", "default", _FAKE_GH_TOKEN)]
    assert _FAKE_GH_TOKEN not in ref.ref


@pytest.mark.unit
def test_keyring_ref_uses_explicit_account() -> None:
    """Verify keyring refs encode the requested account identifier."""
    module = FakeKeyringModule()
    ref = KeyringCredentialBackend(keyring_module=module).create_ref(
        CredentialRequest(
            provider="github",
            account="token",
            secret_source=_secret(_FAKE_GH_TOKEN),
        )
    )
    assert ref.ref == "keyring://awf/github/token"


# --------------------------------------------------------------------------- #
# 2. Unavailable keyring falls back to env_ref (headless-Linux no-keychain).
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.parametrize(
    "module",
    [
        FakeKeyringModule(backend=_FailBackend()),
        FakeKeyringModule(raise_on_get=True),
    ],
)
def test_unavailable_keyring_falls_back_to_env_ref(module: FakeKeyringModule) -> None:
    """Verify a no-op or failing keyring backend yields the env-ref fallback."""
    keyring_backend = KeyringCredentialBackend(keyring_module=module)
    assert keyring_backend.is_available() is False

    selected = select_credential_backend(
        preferred=None,
        capabilities=_HEADLESS_LINUX,
        allow_plain_secrets=False,
        plain_file_consent=False,
        keyring_backend=keyring_backend,
    )
    assert selected.kind == "env_ref"

    ref = selected.create_ref(CredentialRequest(provider="github", env_var="GH_TOKEN"))
    assert ref.ref == "env://GH_TOKEN"


@pytest.mark.unit
def test_explicit_keyring_preference_falls_back_to_env_ref_when_unavailable() -> None:
    """Verify an explicit keyring preference still falls back to env_ref."""
    keyring_backend = KeyringCredentialBackend(keyring_module=FakeKeyringModule(raise_on_get=True))
    selected = select_credential_backend(
        preferred="keyring",
        capabilities=_HEADLESS_LINUX,
        allow_plain_secrets=False,
        plain_file_consent=False,
        keyring_backend=keyring_backend,
    )
    assert selected.kind == "env_ref"


@pytest.mark.unit
@pytest.mark.parametrize("preferred", [None, "keyring"])
def test_store_falls_back_to_env_ref_when_keyring_unusable_at_write_time(
    preferred: str | None,
) -> None:
    """Verify a keyring proven unusable in ``create_ref`` degrades to the env-ref offer.

    ``is_available`` is a cheap pre-filter that only rejects the ``fail``/``null``
    no-op backends, so a headless-Linux ``ChainerBackend`` with no usable child is
    selected as the default keyring backend, yet its write/read-back round-trip in
    ``create_ref`` proves it cannot durably store a secret
    (``CREDENTIAL_BACKEND_UNAVAILABLE``). A keyring detected unusable up front
    already degrades to env_ref; one detected unusable only at write time must
    reach the same env-ref offer (plan R5) instead of failing setup — for both the
    implicit (``None``) and explicit (``"keyring"``) best-effort selections.
    """
    module = FakeKeyringModule(backend=_ChainerBackend(), drop_writes=True)
    keyring_backend = KeyringCredentialBackend(keyring_module=module)
    assert keyring_backend.is_available() is True

    ref = store_provider_credential(
        CredentialRequest(
            provider="github",
            env_var="GH_TOKEN",
            secret_source=_secret(_FAKE_GH_TOKEN),
        ),
        preferred=preferred,
        capabilities=_HEADLESS_LINUX,
        allow_plain_secrets=False,
        plain_file_consent=False,
        keyring_backend=keyring_backend,
    )

    assert ref.backend == "env_ref"
    assert ref.ref == "env://GH_TOKEN"
    # The keyring write was attempted (and proved non-durable) before the fallback,
    # but the secret never reaches the env-ref offer.
    assert module.set_calls == [("awf/github", "default", _FAKE_GH_TOKEN)]
    assert _FAKE_GH_TOKEN not in ref.ref


@pytest.mark.unit
def test_store_keyring_unusable_without_env_var_surfaces_interactive_input() -> None:
    """Verify a write-time-unusable keyring with no env_var mirrors the pre-create path.

    When the keyring is proven unusable at write time and the request carries no
    env_var, the env-ref fallback cannot mint a ref, so it must surface the same
    ``INTERACTIVE_INPUT_REQUIRED`` (missing env_var) a keyring detected unusable up
    front would — an actionable "provide an env var" signal — rather than failing
    with the raw ``CREDENTIAL_BACKEND_UNAVAILABLE``.
    """
    module = FakeKeyringModule(backend=_ChainerBackend(), drop_writes=True)
    keyring_backend = KeyringCredentialBackend(keyring_module=module)

    with pytest.raises(CredentialError) as exc_info:
        store_provider_credential(
            CredentialRequest(provider="github", secret_source=_secret(_FAKE_GH_TOKEN)),
            preferred=None,
            capabilities=_HEADLESS_LINUX,
            allow_plain_secrets=False,
            plain_file_consent=False,
            keyring_backend=keyring_backend,
        )

    error = exc_info.value
    assert error.reason_code == INTERACTIVE_INPUT_REQUIRED
    assert error.details["missing"] == "env_var"
    assert _FAKE_GH_TOKEN not in str(error.to_dict())


@pytest.mark.unit
def test_store_keyring_token_shaped_provider_is_not_masked_by_env_ref() -> None:
    """Verify a rejected token-shaped identifier is never masked by the env-ref fallback.

    The env-ref backend only consumes ``env_var`` (it never touches the provider or
    secret), so a fallback that fired on ``CREDENTIAL_REF_INVALID`` would silently
    emit ``env://NAME`` and hide a provider accidentally populated with a raw
    secret. The fallback must trigger only on the keyring-unusable signal, so this
    pre-write identifier rejection propagates unchanged and the keyring is never
    even written.
    """
    module = FakeKeyringModule()
    keyring_backend = KeyringCredentialBackend(keyring_module=module)

    with pytest.raises(CredentialError) as exc_info:
        store_provider_credential(
            CredentialRequest(
                provider=_FAKE_TOKEN,
                env_var="GH_TOKEN",
                secret_source=_secret(_FAKE_GH_TOKEN),
            ),
            preferred=None,
            capabilities=_HEADLESS_LINUX,
            allow_plain_secrets=False,
            plain_file_consent=False,
            keyring_backend=keyring_backend,
        )

    error = exc_info.value
    assert error.reason_code == CREDENTIAL_REF_INVALID
    assert error.details == {"field": "provider"}
    assert module.set_calls == []
    assert _FAKE_TOKEN not in str(error.to_dict())


@pytest.mark.unit
def test_store_keyring_missing_secret_is_not_masked_by_env_ref() -> None:
    """Verify a usable keyring's missing-secret fault is not masked by the env-ref offer.

    A usable keyring that simply lacks an inline secret raises
    ``INTERACTIVE_INPUT_REQUIRED`` before any write; that is a genuine input fault,
    not the keyring-unusable signal, so it must propagate unchanged even when an
    env_var is present rather than being swapped for a silent ``env://NAME``.
    """
    module = FakeKeyringModule()
    keyring_backend = KeyringCredentialBackend(keyring_module=module)

    with pytest.raises(CredentialError) as exc_info:
        store_provider_credential(
            CredentialRequest(provider="github", env_var="GH_TOKEN", secret_source=None),
            preferred=None,
            capabilities=_HEADLESS_LINUX,
            allow_plain_secrets=False,
            plain_file_consent=False,
            keyring_backend=keyring_backend,
        )

    assert exc_info.value.reason_code == INTERACTIVE_INPUT_REQUIRED
    assert module.set_calls == []


# --------------------------------------------------------------------------- #
# 3. Env ref stores only a variable name.
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.parametrize("env_var", ["OPENAI_API_KEY", "GH_TOKEN"])
def test_env_ref_stores_only_variable_name(env_var: str) -> None:
    """Verify env refs encode only the variable name and store no value."""
    ref = EnvRefCredentialBackend().create_ref(
        CredentialRequest(provider="openai", env_var=env_var)
    )
    assert ref.backend == "env_ref"
    assert ref.ref == f"env://{env_var}"


@pytest.mark.unit
def test_env_ref_select_with_explicit_preference() -> None:
    """Verify env_ref can be selected explicitly without a keyring backend."""
    selected = select_credential_backend(
        preferred="env_ref",
        capabilities=_HEADLESS_LINUX,
        allow_plain_secrets=False,
        plain_file_consent=False,
    )
    assert selected.kind == "env_ref"


@pytest.mark.unit
@pytest.mark.parametrize(
    "env_var",
    [_FAKE_TOKEN, _FAKE_GH_TOKEN, "openai_api_key", "1BAD", "BAD-NAME", "AIza" + "B" * 16],
)
def test_env_ref_rejects_invalid_or_token_shaped_names(env_var: str) -> None:
    """Verify malformed or token-shaped env var names are rejected and redacted."""
    with pytest.raises(CredentialError) as exc_info:
        EnvRefCredentialBackend().create_ref(CredentialRequest(provider="openai", env_var=env_var))

    error = exc_info.value
    assert error.reason_code == CREDENTIAL_REF_INVALID
    assert _FAKE_TOKEN not in str(error.to_dict())
    assert _FAKE_GH_TOKEN not in str(error.to_dict())


# --------------------------------------------------------------------------- #
# 4. Plain-file consent / flag gating.
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.parametrize(
    ("allow_plain_secrets", "consent"),
    [(False, False), (True, False), (False, True)],
)
def test_plain_file_requires_flag_and_consent(
    allow_plain_secrets: bool,
    consent: bool,
    tmp_path: Path,
) -> None:
    """Verify plain-file storage needs both the flag and recorded consent."""
    secrets_dir = tmp_path / "secrets"
    with pytest.raises(CredentialError) as exc_info:
        store_provider_credential(
            CredentialRequest(provider="openai", secret_source=_secret(_FAKE_TOKEN)),
            preferred="plain_file",
            capabilities=_HEADLESS_LINUX,
            allow_plain_secrets=allow_plain_secrets,
            plain_file_consent=consent,
            plain_file_backend=PlainFileCredentialBackend(
                capabilities=_HEADLESS_LINUX,
                allow_plain_secrets=allow_plain_secrets,
                consent=consent,
                secrets_dir=secrets_dir,
            ),
        )

    assert exc_info.value.reason_code == CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED
    assert not secrets_dir.exists()
    assert _FAKE_TOKEN not in str(exc_info.value.to_dict())


@pytest.mark.unit
def test_plain_file_consent_gating_uses_default_backend(tmp_path: Path) -> None:
    """Verify the default plain-file backend is built and gated on consent."""
    with pytest.raises(CredentialError) as exc_info:
        store_provider_credential(
            CredentialRequest(provider="openai", secret_source=_secret(_FAKE_TOKEN)),
            preferred="plain_file",
            capabilities=_HEADLESS_LINUX,
            allow_plain_secrets=False,
            plain_file_consent=False,
        )

    assert exc_info.value.reason_code == CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED


# --------------------------------------------------------------------------- #
# 5. Plain-file permissions and ref shape.
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_plain_file_writes_secret_with_conservative_permissions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify plain-file storage writes a 0600 secret in a 0700 directory."""
    secrets_dir = tmp_path / "secrets"
    ref = store_provider_credential(
        CredentialRequest(provider="openai", secret_source=_secret(_FAKE_TOKEN)),
        preferred="plain_file",
        capabilities=_HEADLESS_LINUX,
        allow_plain_secrets=True,
        plain_file_consent=True,
        plain_file_backend=PlainFileCredentialBackend(
            capabilities=_HEADLESS_LINUX,
            allow_plain_secrets=True,
            consent=True,
            secrets_dir=secrets_dir,
        ),
    )

    secret_file = secrets_dir / "openai.default"
    assert ref.backend == "plain_file"
    assert ref.ref == f"plain-file://{secret_file}"
    assert secret_file.read_text(encoding="utf-8") == _FAKE_TOKEN
    if os.name == "posix":
        assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(secrets_dir.stat().st_mode) == 0o700

    # The path-only ref and config metadata never include the secret value.
    assert _FAKE_TOKEN not in ref.ref
    fields = ref.to_provider_config_fields()
    assert _FAKE_TOKEN not in str(fields)
    assert ProviderConfig(**fields).backend == "plain_file"

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.unit
def test_plain_file_scopes_secret_file_by_account(tmp_path: Path) -> None:
    """Verify plain-file storage scopes the secret file/ref by account.

    Without account scoping, two accounts for one provider would share a single
    ``<provider>`` file and the second write would silently overwrite the first.
    The ref must encode the account (as the keyring backend already does) so each
    account keeps a distinct file and a distinct, non-overwriting reference.
    """
    secrets_dir = tmp_path / "secrets"

    def _store(account: str, secret: str) -> CredentialRef:
        return PlainFileCredentialBackend(
            capabilities=_HEADLESS_LINUX,
            allow_plain_secrets=True,
            consent=True,
            secrets_dir=secrets_dir,
        ).create_ref(
            CredentialRequest(
                provider="openai",
                account=account,
                secret_source=_secret(secret),
            )
        )

    default_ref = _store("default", _FAKE_TOKEN)
    work_ref = _store("work", _FAKE_GH_TOKEN)

    default_file = secrets_dir / "openai.default"
    work_file = secrets_dir / "openai.work"
    assert default_ref.ref == f"plain-file://{default_file}"
    assert work_ref.ref == f"plain-file://{work_file}"
    assert default_ref.ref != work_ref.ref
    # The first account's secret survives the second account's write.
    assert default_file.read_text(encoding="utf-8") == _FAKE_TOKEN
    assert work_file.read_text(encoding="utf-8") == _FAKE_GH_TOKEN


@pytest.mark.unit
def test_plain_file_strips_secret_whitespace_before_storage(tmp_path: Path) -> None:
    """Verify a whitespace-padded secret is stripped before the plain-file write.

    Surrounding whitespace would otherwise be written to the ``0600`` secret file
    verbatim and silently break authentication later, so the stored value is
    normalised the same way the keyring path normalises it.
    """
    secrets_dir = tmp_path / "secrets"
    ref = PlainFileCredentialBackend(
        capabilities=_HEADLESS_LINUX,
        allow_plain_secrets=True,
        consent=True,
        secrets_dir=secrets_dir,
    ).create_ref(
        CredentialRequest(
            provider="openai",
            secret_source=_secret(f"\t{_FAKE_TOKEN}\n"),
        )
    )

    target = secrets_dir / "openai.default"
    assert ref.ref == f"plain-file://{target}"
    assert target.read_text(encoding="utf-8") == _FAKE_TOKEN


@pytest.mark.unit
def test_plain_file_rejects_token_shaped_account(tmp_path: Path) -> None:
    """Verify a token-shaped account is refused before any plain-file write.

    The account is interpolated into the secret file path for multi-account
    scoping, so a token-shaped value must be rejected with a secret-free reason
    code and leave neither the secret file nor the secrets directory behind.
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
            CredentialRequest(
                provider="openai",
                account=_FAKE_GH_TOKEN,
                secret_source=_secret(_FAKE_TOKEN),
            )
        )

    error = exc_info.value
    assert error.reason_code == CREDENTIAL_REF_INVALID
    assert error.details == {"field": "account"}
    assert not secrets_dir.exists()
    assert _FAKE_TOKEN not in str(error.to_dict())
    assert _FAKE_GH_TOKEN not in str(error.to_dict())


@pytest.mark.unit
def test_plain_file_creates_secrets_dir_restrictively(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify the secrets dir is *created* 0700, not loosened-then-tightened.

    ``mkdir`` honours the caller's umask, so a permissive umask would otherwise
    expose the directory listing (provider names/structure) until the later
    ``_chmod_best_effort`` tightens it — a TOCTOU window on multi-user hosts.
    Neutralise the post-``mkdir`` chmod and use a fully permissive umask so the
    only thing that can yield a 0700 directory is a restrictive create-time mode.
    """
    if os.name != "posix":
        pytest.skip("directory permission semantics are POSIX-specific")

    monkeypatch.setattr(credentials, "_chmod_best_effort", lambda *_a, **_k: None)
    secrets_dir = tmp_path / "secrets"
    old_umask = os.umask(0o000)
    try:
        store_provider_credential(
            CredentialRequest(provider="openai", secret_source=_secret(_FAKE_TOKEN)),
            preferred="plain_file",
            capabilities=_HEADLESS_LINUX,
            allow_plain_secrets=True,
            plain_file_consent=True,
            plain_file_backend=PlainFileCredentialBackend(
                capabilities=_HEADLESS_LINUX,
                allow_plain_secrets=True,
                consent=True,
                secrets_dir=secrets_dir,
            ),
        )
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE(secrets_dir.stat().st_mode) == 0o700


@pytest.mark.unit
def test_plain_file_creates_intermediate_parents_restrictively(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify intermediate parents (e.g. ``~/.awf``) are also *created* 0700.

    ``Path.mkdir(parents=True, mode=0o700)`` applies the restrictive mode only to
    the leaf; the intermediate parent of ``~/.awf/secrets`` would otherwise be
    created with umask-default permissions, leaving ``~/.awf`` world-traversable
    on a multi-user host. Use a not-yet-existing parent (``awf``) so the secrets
    dir's creation must walk through it, neutralise the post-``mkdir`` chmod, and
    use a fully permissive umask so the only thing that can yield a 0700 parent is
    a restrictive create-time mode.
    """
    if os.name != "posix":
        pytest.skip("directory permission semantics are POSIX-specific")

    monkeypatch.setattr(credentials, "_chmod_best_effort", lambda *_a, **_k: None)
    parent_dir = tmp_path / "awf"
    secrets_dir = parent_dir / "secrets"
    old_umask = os.umask(0o000)
    try:
        store_provider_credential(
            CredentialRequest(provider="openai", secret_source=_secret(_FAKE_TOKEN)),
            preferred="plain_file",
            capabilities=_HEADLESS_LINUX,
            allow_plain_secrets=True,
            plain_file_consent=True,
            plain_file_backend=PlainFileCredentialBackend(
                capabilities=_HEADLESS_LINUX,
                allow_plain_secrets=True,
                consent=True,
                secrets_dir=secrets_dir,
            ),
        )
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE(secrets_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(parent_dir.stat().st_mode) == 0o700


@pytest.mark.unit
def test_plain_file_backend_defaults_to_awf_secrets_dir() -> None:
    """Verify the default plain-file secrets directory is ``~/.awf/secrets``."""
    backend = PlainFileCredentialBackend(
        capabilities=_HEADLESS_LINUX,
        allow_plain_secrets=True,
        consent=True,
    )
    assert backend._secrets_dir.name == "secrets"
    assert backend._secrets_dir.parent.name == ".awf"


@pytest.mark.unit
def test_plain_file_backend_resolves_relative_secrets_dir() -> None:
    """Verify a relative ``secrets_dir`` is resolved to an absolute path.

    Plain-file refs must be ``plain-file://<abs-path>``; a relative input would
    otherwise yield a relative ref that breaks if the working directory changes.
    """
    backend = PlainFileCredentialBackend(
        capabilities=_HEADLESS_LINUX,
        allow_plain_secrets=True,
        consent=True,
        secrets_dir="relative/secrets",
    )
    assert backend._secrets_dir.is_absolute()
    assert backend._secrets_dir == Path("relative/secrets").resolve()


@pytest.mark.unit
def test_plain_file_write_failure_is_reason_coded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify low-level write failures map to a secret-free reason code."""

    def _open_fails(*args: object, **kwargs: object) -> int:
        """Raise an `OSError` to simulate a failed atomic secret write."""
        raise OSError("disk full")

    monkeypatch.setattr(credentials.os, "open", _open_fails)

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
    assert "disk full" not in str(error.to_dict())
    assert _FAKE_TOKEN not in str(error.to_dict())


# --------------------------------------------------------------------------- #
# 6 & 7. Non-Linux and non-headless rejection for plain-file.
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.parametrize("capabilities", [_MACOS, _WINDOWS, _DESKTOP_LINUX])
def test_plain_file_rejected_on_unsupported_hosts(
    capabilities: HostCredentialCapabilities,
    tmp_path: Path,
) -> None:
    """Verify plain-file storage is refused off headless Linux even with consent."""
    secrets_dir = tmp_path / "secrets"
    backend = PlainFileCredentialBackend(
        capabilities=capabilities,
        allow_plain_secrets=True,
        consent=True,
        secrets_dir=secrets_dir,
    )
    assert backend.is_available() is False

    with pytest.raises(CredentialError) as exc_info:
        backend.create_ref(CredentialRequest(provider="openai", secret_source=_secret(_FAKE_TOKEN)))

    error = exc_info.value
    assert error.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE
    assert not secrets_dir.exists()
    assert _FAKE_TOKEN not in str(error.to_dict())


@pytest.mark.unit
@pytest.mark.parametrize("capabilities", [_MACOS, _WINDOWS, _DESKTOP_LINUX])
def test_plain_file_unsupported_host_reports_platform_before_consent(
    capabilities: HostCredentialCapabilities,
    tmp_path: Path,
) -> None:
    """Verify an unsupported host reports BACKEND_UNAVAILABLE even with no consent.

    The platform restriction is the more fundamental gate, so a caller on macOS
    or desktop Linux who also has not set the flag/consent should learn the host
    is unsupported rather than be sent down the consent path — adding the flag
    and consent on that host would still be rejected.
    """
    secrets_dir = tmp_path / "secrets"
    backend = PlainFileCredentialBackend(
        capabilities=capabilities,
        allow_plain_secrets=False,
        consent=False,
        secrets_dir=secrets_dir,
    )

    with pytest.raises(CredentialError) as exc_info:
        backend.create_ref(CredentialRequest(provider="openai", secret_source=_secret(_FAKE_TOKEN)))

    error = exc_info.value
    assert error.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE
    assert not secrets_dir.exists()
    assert _FAKE_TOKEN not in str(error.to_dict())
