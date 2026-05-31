"""Non-interactive enforcement, redaction, capability detection, config tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import awf.host_setup as host_setup
from awf.host_setup import read_host_setup_config, write_host_setup_config
from awf.host_setup.config import ProviderConfig, default_host_setup_config_path
from awf.host_setup.credentials import (
    CREDENTIAL_BACKEND_UNAVAILABLE,
    CREDENTIAL_BACKENDS,
    INTERACTIVE_INPUT_REQUIRED,
    CredentialError,
    CredentialRef,
    CredentialRequest,
    EnvRefCredentialBackend,
    KeyringCredentialBackend,
    PlainFileCredentialBackend,
    detect_host_credential_capabilities,
    store_provider_credential,
)
from awf.host_setup.rendering import redact_first_run_value
from tests.unit.service.test_host_setup_credentials_parts._helpers import (
    _FAKE_GH_TOKEN,
    _FAKE_TOKEN,
    _HEADLESS_LINUX,
    FakeKeyringModule,
    _secret,
)


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
@pytest.mark.parametrize("secret", ["   ", "\t\n"])
def test_keyring_whitespace_only_secret_is_interactive_input_required(secret: str) -> None:
    """Verify a whitespace-only secret is treated as missing input.

    A whitespace-only value is truthy, so it would otherwise slip past the
    ``not secret`` guard and be written to the keychain as an unusable
    credential. It must instead raise ``INTERACTIVE_INPUT_REQUIRED`` and never
    reach the backend, mirroring the env_ref name guard.
    """
    fake_keyring = FakeKeyringModule()
    backend = KeyringCredentialBackend(keyring_module=fake_keyring)
    with pytest.raises(CredentialError) as exc_info:
        backend.create_ref(CredentialRequest(provider="github", secret_source=_secret(secret)))

    assert exc_info.value.reason_code == INTERACTIVE_INPUT_REQUIRED
    assert fake_keyring.set_calls == []


@pytest.mark.unit
def test_keyring_strips_secret_whitespace_before_storage() -> None:
    """Verify a whitespace-padded secret is stripped before the keychain write.

    A secret padded with leading/trailing whitespace is truthy and survives the
    whitespace-only guard, but the surrounding whitespace would make the stored
    credential fail silently at authentication time. It must be stripped before
    storage, mirroring the env_ref path which strips its identifier before use.
    """
    module = FakeKeyringModule()
    backend = KeyringCredentialBackend(keyring_module=module)

    backend.create_ref(
        CredentialRequest(
            provider="github",
            secret_source=_secret(f"  {_FAKE_GH_TOKEN}  "),
        )
    )

    # The padded value is normalised to the bare token before it reaches storage.
    assert module.set_calls == [("awf/github", "default", _FAKE_GH_TOKEN)]


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

    error = exc_info.value
    assert error.reason_code == INTERACTIVE_INPUT_REQUIRED
    # A legitimate provider name is preserved verbatim in the diagnostic.
    assert error.details["provider"] == "openai"


@pytest.mark.unit
@pytest.mark.parametrize("env_var", ["", "   ", "\t\n"])
def test_env_ref_blank_variable_is_interactive_input_required(env_var: str) -> None:
    """Verify a blank/whitespace-only env var name is treated as missing input.

    A whitespace-only name strips to ``""``, which is semantically "no name
    provided", so it must raise ``INTERACTIVE_INPUT_REQUIRED`` (missing input)
    rather than the ``CREDENTIAL_REF_INVALID`` an empty post-strip identifier
    would otherwise hit.
    """
    with pytest.raises(CredentialError) as exc_info:
        EnvRefCredentialBackend().create_ref(CredentialRequest(provider="openai", env_var=env_var))

    error = exc_info.value
    assert error.reason_code == INTERACTIVE_INPUT_REQUIRED
    assert error.details["missing"] == "env_var"


@pytest.mark.unit
def test_env_ref_missing_variable_redacts_token_shaped_provider() -> None:
    """Verify a token-shaped provider never surfaces when env input is missing.

    The env_ref backend raises ``INTERACTIVE_INPUT_REQUIRED`` for a missing env
    var name without first routing ``provider`` through ``_require_safe_identifier``
    (env_ref never interpolates the provider, so it has no other validation). A
    provider accidentally populated with a raw secret must therefore be redacted
    out of the error ``details``/``to_dict()`` rather than echoed verbatim.
    """
    with pytest.raises(CredentialError) as exc_info:
        EnvRefCredentialBackend().create_ref(CredentialRequest(provider=_FAKE_TOKEN, env_var=None))

    error = exc_info.value
    assert error.reason_code == INTERACTIVE_INPUT_REQUIRED
    assert error.details["missing"] == "env_var"
    assert _FAKE_TOKEN not in str(error.details["provider"])
    assert _FAKE_TOKEN not in str(error.to_dict())


@pytest.mark.unit
@pytest.mark.parametrize(
    "provider",
    [
        _FAKE_TOKEN.upper(),
        _FAKE_GH_TOKEN.upper(),
        "sK-pRoJ-" + ("a" * 48),
    ],
)
def test_env_ref_missing_variable_redacts_case_variant_token_provider(provider: str) -> None:
    """Verify a case-variant token-shaped provider is redacted, not echoed verbatim.

    This module's own token-shape checks (``_TOKEN_SHAPE_RE``) are
    case-insensitive, so the diagnostic redaction on the unvalidated env_ref
    missing-input path must fold case too. A case-variant token-shaped provider
    (e.g. ``SK-PROJ-...``) must not slip past the case-sensitive audit helper and
    surface verbatim in the error ``details``/``to_dict()``.
    """
    with pytest.raises(CredentialError) as exc_info:
        EnvRefCredentialBackend().create_ref(CredentialRequest(provider=provider, env_var=None))

    error = exc_info.value
    assert error.reason_code == INTERACTIVE_INPUT_REQUIRED
    assert error.details["missing"] == "env_var"
    assert provider not in str(error.details["provider"])
    assert provider not in str(error.to_dict())


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
