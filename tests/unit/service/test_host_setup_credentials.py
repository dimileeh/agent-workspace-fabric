"""Credential backend abstraction tests for AWF first-run host setup.

These tests never touch the real OS keychain or the real ``keyring`` library:
keyring access is exercised through injected fakes and ``importlib`` patching,
and the plain-file backend writes only under ``tmp_path``. Every assertion
checks that a fixed fake token never escapes into refs, error details,
``to_dict()`` payloads, redacted output, or captured stdout/stderr.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

import awf.host_setup as host_setup
import awf.host_setup.credentials as credentials
from awf.host_setup import read_host_setup_config, write_host_setup_config
from awf.host_setup.config import ProviderConfig, default_host_setup_config_path
from awf.host_setup.credentials import (
    CREDENTIAL_BACKEND_UNAVAILABLE,
    CREDENTIAL_BACKENDS,
    CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED,
    CREDENTIAL_REF_INVALID,
    INTERACTIVE_INPUT_REQUIRED,
    MAX_CREDENTIAL_REF_LENGTH,
    CredentialError,
    CredentialRef,
    CredentialRequest,
    EnvRefCredentialBackend,
    HostCredentialCapabilities,
    KeyringCredentialBackend,
    PlainFileCredentialBackend,
    detect_host_credential_capabilities,
    select_credential_backend,
    store_provider_credential,
)
from awf.host_setup.rendering import redact_first_run_value

# Fixed fake credential values; tests assert these never leak into outputs.
_FAKE_TOKEN = "sk-proj-" + ("a" * 48)
_FAKE_GH_TOKEN = "ghp_" + ("b" * 36)

_HEADLESS_LINUX = HostCredentialCapabilities(os_name="Linux", is_headless=True)
_DESKTOP_LINUX = HostCredentialCapabilities(os_name="Linux", is_headless=False)
_MACOS = HostCredentialCapabilities(os_name="Darwin", is_headless=False)
_WINDOWS = HostCredentialCapabilities(os_name="Windows", is_headless=False)


class _FakeKeyringError(Exception):
    """Stand-in for ``keyring.errors.KeyringError`` used by fakes."""


class _FakeKeyringErrors:
    """Namespace mirroring the real ``keyring.errors`` module surface."""

    KeyringError = _FakeKeyringError


class _UsableBackend:
    """Keyring backend whose module name does not denote a no-op backend."""


class _FailBackend:
    """Keyring backend that mimics ``keyring.backends.fail.Keyring``."""


# Mimic the import location the real no-op backend lives in.
_FailBackend.__module__ = "keyring.backends.fail"


class FakeKeyringModule:
    """Injectable fake keyring module that records secrets in memory only."""

    def __init__(
        self,
        *,
        backend: object | None = None,
        raise_on_get: bool = False,
        raise_on_set: bool = False,
        get_error: BaseException | None = None,
        set_error: BaseException | None = None,
    ) -> None:
        """Configure a fake keyring backend without touching any real keychain."""
        self._backend: object = _UsableBackend() if backend is None else backend
        self._raise_on_get = raise_on_get
        self._raise_on_set = raise_on_set
        self._get_error = get_error
        self._set_error = set_error
        self.errors = _FakeKeyringErrors
        self.set_calls: list[tuple[str, str, str]] = []

    def get_keyring(self) -> object:
        """Return the configured backend or raise like a missing keychain."""
        if self._get_error is not None:
            raise self._get_error
        if self._raise_on_get:
            raise _FakeKeyringError("no usable keyring backend")
        return self._backend

    def set_password(self, service: str, username: str, password: str) -> None:
        """Record a stored secret or raise like a locked/broken keychain."""
        if self._set_error is not None:
            raise self._set_error
        if self._raise_on_set:
            raise _FakeKeyringError("keychain locked")
        self.set_calls.append((service, username, password))


def _secret(value: str | None) -> Callable[[], str | None]:
    """Return a lazy secret source callable yielding ``value``."""
    return lambda: value


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

    secret_file = secrets_dir / "openai"
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


# --------------------------------------------------------------------------- #
# 8. Non-interactive enforcement for missing input.
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_keyring_missing_secret_is_interactive_input_required() -> None:
    """Verify keyring storage with no secret source fails non-interactively."""
    backend = KeyringCredentialBackend(keyring_module=FakeKeyringModule())
    with pytest.raises(CredentialError) as exc_info:
        backend.create_ref(CredentialRequest(provider="github", secret_source=None))

    assert exc_info.value.reason_code == INTERACTIVE_INPUT_REQUIRED


@pytest.mark.unit
def test_keyring_empty_secret_is_interactive_input_required() -> None:
    """Verify an empty secret value is treated as missing input."""
    backend = KeyringCredentialBackend(keyring_module=FakeKeyringModule())
    with pytest.raises(CredentialError) as exc_info:
        backend.create_ref(CredentialRequest(provider="github", secret_source=_secret("")))

    assert exc_info.value.reason_code == INTERACTIVE_INPUT_REQUIRED


@pytest.mark.unit
def test_plain_file_missing_secret_is_interactive_input_required(tmp_path: Path) -> None:
    """Verify plain-file storage with no secret source fails non-interactively."""
    secrets_dir = tmp_path / "secrets"
    backend = PlainFileCredentialBackend(
        capabilities=_HEADLESS_LINUX,
        allow_plain_secrets=True,
        consent=True,
        secrets_dir=secrets_dir,
    )
    with pytest.raises(CredentialError) as exc_info:
        backend.create_ref(CredentialRequest(provider="openai", secret_source=None))

    assert exc_info.value.reason_code == INTERACTIVE_INPUT_REQUIRED
    assert not secrets_dir.exists()


@pytest.mark.unit
def test_env_ref_missing_variable_is_interactive_input_required() -> None:
    """Verify env refs without a variable name fail non-interactively."""
    with pytest.raises(CredentialError) as exc_info:
        EnvRefCredentialBackend().create_ref(CredentialRequest(provider="openai", env_var=None))

    assert exc_info.value.reason_code == INTERACTIVE_INPUT_REQUIRED


# --------------------------------------------------------------------------- #
# 9. Redaction: token-shaped inputs never surface.
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_token_inputs_never_appear_in_outputs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify token-shaped secrets never appear in refs, errors, or output."""
    keyring_ref = store_provider_credential(
        CredentialRequest(provider="github", secret_source=_secret(_FAKE_GH_TOKEN)),
        preferred="keyring",
        capabilities=_HEADLESS_LINUX,
        allow_plain_secrets=False,
        plain_file_consent=False,
        keyring_backend=KeyringCredentialBackend(keyring_module=FakeKeyringModule()),
    )
    plain_ref = store_provider_credential(
        CredentialRequest(provider="openai", secret_source=_secret(_FAKE_TOKEN)),
        preferred="plain_file",
        capabilities=_HEADLESS_LINUX,
        allow_plain_secrets=True,
        plain_file_consent=True,
        plain_file_backend=PlainFileCredentialBackend(
            capabilities=_HEADLESS_LINUX,
            allow_plain_secrets=True,
            consent=True,
            secrets_dir=tmp_path / "secrets",
        ),
    )

    for ref in (keyring_ref, plain_ref):
        assert _FAKE_TOKEN not in ref.ref
        assert _FAKE_GH_TOKEN not in ref.ref

    rendered = redact_first_run_value(
        {
            "providers": {
                "github": {"credential_ref": keyring_ref.ref},
                "openai": {"credential_ref": plain_ref.ref},
            }
        }
    )
    assert _FAKE_TOKEN not in str(rendered)
    assert _FAKE_GH_TOKEN not in str(rendered)
    # Provider refs are redacted to a stable marker, not echoed verbatim.
    assert keyring_ref.ref not in str(rendered)
    assert plain_ref.ref not in str(rendered)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.unit
def test_credential_error_to_dict_round_trips_without_details() -> None:
    """Verify credential errors omit empty details and expose reason codes."""
    error = CredentialError(
        reason_code=CREDENTIAL_BACKEND_UNAVAILABLE,
        message="No usable credential backend.",
    )
    assert error.to_dict() == {
        "status": "failed",
        "reason_code": CREDENTIAL_BACKEND_UNAVAILABLE,
        "message": "No usable credential backend.",
    }


# --------------------------------------------------------------------------- #
# 10. Host capability detection.
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.parametrize(
    ("system", "environ", "expected_headless", "expected_plain_file"),
    [
        ("Linux", {}, True, True),
        ("Linux", {"DISPLAY": ":0"}, False, False),
        ("Linux", {"WAYLAND_DISPLAY": "wayland-0"}, False, False),
        ("Darwin", {}, False, False),
        ("Windows", {}, False, False),
    ],
)
def test_detect_host_credential_capabilities(
    system: str,
    environ: dict[str, str],
    expected_headless: bool,
    expected_plain_file: bool,
) -> None:
    """Verify host capability detection classifies hosts deterministically."""
    capabilities = detect_host_credential_capabilities(system=system, environ=environ)
    assert capabilities.os_name == system
    assert capabilities.is_headless is expected_headless
    assert capabilities.supports_plain_file is expected_plain_file


@pytest.mark.unit
def test_detect_host_credential_capabilities_uses_live_defaults() -> None:
    """Verify detection falls back to the live platform and environment."""
    capabilities = detect_host_credential_capabilities()
    assert capabilities.os_name
    assert isinstance(capabilities.is_headless, bool)


# --------------------------------------------------------------------------- #
# 11. Config integration and metadata round-trip.
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_credential_ref_builds_provider_config_fields() -> None:
    """Verify credential refs map to provider config fields with a default status."""
    ref = CredentialRef(backend="env_ref", ref="env://GH_TOKEN")
    assert ref.to_provider_config_fields() == {
        "credential_ref": "env://GH_TOKEN",
        "backend": "env_ref",
        "status": "ready",
    }
    assert ref.to_provider_config_fields(status="configured")["status"] == "configured"


@pytest.mark.unit
def test_provider_config_round_trips_backend_metadata(tmp_path: Path) -> None:
    """Verify backend metadata persists through write/read round-trips."""
    ref = CredentialRef(backend="keyring", ref="keyring://awf/github/token")
    config_path = default_host_setup_config_path(home=tmp_path / "home")

    write_host_setup_config(
        host_setup.HostSetupConfig(
            providers={"github": ProviderConfig(**ref.to_provider_config_fields())}
        ),
        path=config_path,
    )

    loaded = read_host_setup_config(path=config_path)
    provider = loaded.providers["github"]
    assert provider.credential_ref == "keyring://awf/github/token"
    assert provider.backend == "keyring"
    assert provider.status == "ready"


@pytest.mark.unit
def test_provider_config_backend_metadata_is_optional() -> None:
    """Verify the backend metadata field stays optional for back-compat."""
    assert ProviderConfig(credential_ref="env://GH_TOKEN").backend is None
    assert ProviderConfig(credential_ref="env://GH_TOKEN", backend=None).backend is None


@pytest.mark.unit
def test_provider_config_rejects_unknown_backend() -> None:
    """Verify provider config rejects unknown backend metadata."""
    with pytest.raises(ValidationError):
        ProviderConfig(credential_ref="env://GH_TOKEN", backend="bogus")


@pytest.mark.unit
def test_provider_config_still_rejects_raw_secret_credential_ref() -> None:
    """Verify the existing raw-secret guard still holds with backend metadata."""
    with pytest.raises(ValidationError):
        ProviderConfig(credential_ref=_FAKE_GH_TOKEN, backend="env_ref")


@pytest.mark.unit
def test_credential_backends_match_provider_config_vocabulary() -> None:
    """Verify the backend kinds stay in lockstep with provider config validation."""
    assert CREDENTIAL_BACKENDS == ("keyring", "env_ref", "plain_file")
    for backend in CREDENTIAL_BACKENDS:
        assert ProviderConfig(credential_ref="env://GH_TOKEN", backend=backend).backend == backend


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
def test_host_setup_reexports_credential_symbols() -> None:
    """Verify host setup re-exports the public credential surface additively."""
    assert host_setup.CredentialError is CredentialError
    assert host_setup.CredentialRef is CredentialRef
    assert host_setup.store_provider_credential is store_provider_credential
    assert host_setup.detect_host_credential_capabilities is (detect_host_credential_capabilities)
