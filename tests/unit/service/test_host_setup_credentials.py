"""Host setup credential backend tests (keyring / env_ref / plain_file)."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path

import pytest
import structlog
from pydantic import ValidationError

import awf.host_setup as host_setup
import awf.host_setup.credentials as credentials
from awf.host_setup import (
    HostSetupConfig,
    ProviderConfig,
    read_host_setup_config,
    write_host_setup_config,
)
from awf.host_setup.credentials import (
    CredentialRef,
    EnvRefBackend,
    HostFacts,
    HostSetupCredentialError,
    KeyringBackend,
    PlainFileBackend,
    SecretValue,
    detect_host_facts,
    get_provider_credential_ref,
    offered_credential_backends,
    resolve_credential_ref,
    select_credential_backend,
    set_provider_credential_ref,
    store_provider_credential,
)
from awf.host_setup.rendering import (
    CREDENTIAL_BACKEND_UNAVAILABLE,
    CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED,
    CREDENTIAL_REF_INVALID,
    INTERACTIVE_INPUT_REQUIRED,
)

_FAKE_OPENAI_TOKEN = "sk-proj-" + ("a" * 48)
_FAKE_ANTHROPIC_TOKEN = "sk-ant-" + ("b" * 40)
_FAKE_GITHUB_TOKEN = "ghp_" + ("c" * 36)
_FAKE_PAT_TOKEN = "github_pat_" + ("d" * 40)


class _FailBackend:
    """Stand-in keyring backend whose module marks it unusable."""


_FailBackend.__module__ = "keyring.backends.fail"


class _RealLikeBackend:
    """Stand-in keyring backend that looks like a usable backend."""


class FakeKeyring:
    """In-memory keyring module replacement for deterministic tests."""

    def __init__(self, *, available: bool = True) -> None:
        """Build an in-memory keyring with a controllable availability flag."""
        self.available = available
        self.store: dict[tuple[str, str], str] = {}
        self.read_count = 0

    def get_keyring(self) -> object:
        """Return a fail or usable backend depending on availability."""
        return _RealLikeBackend() if self.available else _FailBackend()

    def set_password(self, service: str, username: str, password: str) -> None:
        """Record a password in the in-memory store."""
        self.store[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        """Return a recorded password, tracking read access."""
        self.read_count += 1
        return self.store.get((service, username))


def _make_host_facts(*, os_name: str = "Linux", headless: bool = True) -> HostFacts:
    """Build deterministic host facts for platform-branch tests."""
    return HostFacts(os_name=os_name, is_linux=os_name == "Linux", is_headless=headless)


def _keyring_backend(*, available: bool = True) -> KeyringBackend:
    """Build a keyring backend wired to an in-memory fake module."""
    fake = FakeKeyring(available=available)
    return KeyringBackend(module_loader=lambda: fake)


# --- AC#1: keyring is the default when available -------------------------------


@pytest.mark.unit
def test_keyring_is_default_when_available() -> None:
    """Verify keyring is selected by default when the backend is available."""
    facts = _make_host_facts()
    backend = select_credential_backend(facts=facts, keyring_backend=_keyring_backend())

    assert backend.name == "keyring"


@pytest.mark.unit
def test_select_credential_backend_honors_env_ref_request() -> None:
    """Verify an explicit env-ref request selects the env-ref backend."""
    facts = _make_host_facts()

    backend = select_credential_backend(facts=facts, requested="env_ref")

    assert backend.name == "env_ref"


@pytest.mark.unit
def test_credential_error_to_dict_omits_empty_details() -> None:
    """Verify credential errors omit an empty details map from their payload."""
    error = HostSetupCredentialError(
        reason_code=CREDENTIAL_REF_INVALID,
        message="AWF rejected a malformed provider credential reference.",
    )

    assert error.to_dict() == {
        "status": "failed",
        "reason_code": CREDENTIAL_REF_INVALID,
        "message": "AWF rejected a malformed provider credential reference.",
    }


# --- AC#5: headless Linux without keychain offers env or plain -----------------


@pytest.mark.unit
def test_keyring_unavailable_offers_env_or_plain_on_headless_linux() -> None:
    """Verify fallback offers include env always and plain-file only when gated."""
    facts = _make_host_facts(os_name="Linux", headless=True)

    assert offered_credential_backends(
        facts,
        allow_plain_secrets=False,
        plain_file_consent=False,
    ) == ("env_ref",)
    assert offered_credential_backends(
        facts,
        allow_plain_secrets=True,
        plain_file_consent=True,
    ) == ("env_ref", "plain_file")


@pytest.mark.unit
def test_keyring_unavailable_no_fallback_selected() -> None:
    """Verify selecting nothing when keyring is unavailable is reason-coded."""
    facts = _make_host_facts()

    with pytest.raises(HostSetupCredentialError) as exc_info:
        select_credential_backend(facts=facts, keyring_backend=_keyring_backend(available=False))

    error = exc_info.value
    assert error.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE
    assert error.to_dict()["details"]["offered_backends"] == ["env_ref"]


# --- AC#2: env ref stores only the variable name -------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("var_name", ["OPENAI_API_KEY", "GH_TOKEN"])
def test_env_ref_stores_only_variable_name(var_name: str) -> None:
    """Verify env-ref backend records only the variable name, never the value."""
    backend = EnvRefBackend()

    ref = backend.store(var_name, SecretValue(_FAKE_OPENAI_TOKEN))

    assert ref.backend == "env_ref"
    assert ref.to_ref_string() == f"env://{var_name}"
    assert _FAKE_OPENAI_TOKEN not in ref.to_ref_string()
    assert _FAKE_OPENAI_TOKEN not in repr(ref)


@pytest.mark.unit
def test_env_ref_rejects_non_env_var_names() -> None:
    """Verify env-ref backend rejects names that are not env-var shaped."""
    backend = EnvRefBackend()

    with pytest.raises(HostSetupCredentialError) as exc_info:
        backend.store("lower-case", SecretValue("unused"))

    assert exc_info.value.reason_code == CREDENTIAL_REF_INVALID


@pytest.mark.unit
def test_env_ref_resolves_from_injected_environment() -> None:
    """Verify env-ref backend resolves the value from the environment by name."""
    backend = EnvRefBackend(environ={"OPENAI_API_KEY": _FAKE_OPENAI_TOKEN})
    ref = backend.store("OPENAI_API_KEY", SecretValue("unused"))

    resolved = backend.resolve(ref)
    assert resolved is not None
    assert resolved.reveal() == _FAKE_OPENAI_TOKEN
    assert backend.resolve(CredentialRef(backend="env_ref", key="MISSING_VAR")) is None


# --- AC#3 / AC#4: plain-file gating --------------------------------------------


@pytest.mark.unit
def test_plain_file_requires_allow_flag(tmp_path: Path) -> None:
    """Verify plain-file selection without the allow flag is reason-coded."""
    facts = _make_host_facts(os_name="Linux", headless=True)

    with pytest.raises(HostSetupCredentialError) as exc_info:
        select_credential_backend(
            facts=facts,
            requested="plain_file",
            allow_plain_secrets=False,
            plain_file_consent=True,
            plain_file_dir=tmp_path,
        )

    assert exc_info.value.reason_code == CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED


@pytest.mark.unit
def test_plain_file_requires_consent(tmp_path: Path) -> None:
    """Verify plain-file selection without recorded consent is reason-coded."""
    facts = _make_host_facts(os_name="Linux", headless=True)

    with pytest.raises(HostSetupCredentialError) as exc_info:
        select_credential_backend(
            facts=facts,
            requested="plain_file",
            allow_plain_secrets=True,
            plain_file_consent=False,
            plain_file_dir=tmp_path,
        )

    assert exc_info.value.reason_code == CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED


@pytest.mark.unit
@pytest.mark.parametrize("os_name", ["Darwin", "Windows"])
def test_plain_file_rejected_on_non_linux(os_name: str, tmp_path: Path) -> None:
    """Verify plain-file is rejected on non-Linux hosts despite allow + consent."""
    facts = _make_host_facts(os_name=os_name, headless=True)

    with pytest.raises(HostSetupCredentialError) as exc_info:
        select_credential_backend(
            facts=facts,
            requested="plain_file",
            allow_plain_secrets=True,
            plain_file_consent=True,
            plain_file_dir=tmp_path,
        )

    assert exc_info.value.reason_code == CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED


@pytest.mark.unit
def test_plain_file_rejected_on_non_headless_linux(tmp_path: Path) -> None:
    """Verify plain-file is rejected on a desktop Linux host with allow + consent."""
    facts = _make_host_facts(os_name="Linux", headless=False)

    with pytest.raises(HostSetupCredentialError) as exc_info:
        select_credential_backend(
            facts=facts,
            requested="plain_file",
            allow_plain_secrets=True,
            plain_file_consent=True,
            plain_file_dir=tmp_path,
        )

    assert exc_info.value.reason_code == CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED


@pytest.mark.unit
def test_plain_file_requires_target_dir() -> None:
    """Verify selecting plain-file without a target dir is reason-coded."""
    facts = _make_host_facts(os_name="Linux", headless=True)

    with pytest.raises(HostSetupCredentialError) as exc_info:
        select_credential_backend(
            facts=facts,
            requested="plain_file",
            allow_plain_secrets=True,
            plain_file_consent=True,
            plain_file_dir=None,
        )

    assert exc_info.value.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE


@pytest.mark.unit
def test_plain_file_allowed_on_headless_linux_with_allow_and_consent(tmp_path: Path) -> None:
    """Verify gated plain-file storage writes a 0600 file with a path ref."""
    facts = _make_host_facts(os_name="Linux", headless=True)
    secret_dir = tmp_path / "secrets"

    with structlog.testing.capture_logs() as captured:
        ref = store_provider_credential(
            logical_name="github",
            secret=SecretValue(_FAKE_GITHUB_TOKEN),
            facts=facts,
            requested_backend="plain_file",
            allow_plain_secrets=True,
            plain_file_consent=True,
            plain_file_dir=secret_dir,
        )

    assert ref.backend == "plain_file"
    secret_path = Path(ref.key)
    assert secret_path.is_file()
    assert secret_path.read_text(encoding="utf-8") == _FAKE_GITHUB_TOKEN
    assert ref.to_ref_string().startswith("plain-file://")
    ProviderConfig(credential_ref=ref.to_ref_string())
    if os.name == "posix":
        assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600
    assert _FAKE_GITHUB_TOKEN not in json.dumps(captured)


# --- AC#7: non-interactive behavior --------------------------------------------


@pytest.mark.unit
def test_missing_secret_input_is_non_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify a value-requiring backend with no secret never prompts."""
    facts = _make_host_facts()
    prompted: list[object] = []

    def _input_should_not_run(*args: object, **kwargs: object) -> str:
        """Record any prompt attempt so the test can assert it never happens."""
        prompted.append((args, kwargs))
        raise AssertionError("non-interactive setup must not prompt for input")

    monkeypatch.setattr("builtins.input", _input_should_not_run)

    with pytest.raises(HostSetupCredentialError) as exc_info:
        store_provider_credential(
            logical_name="github",
            secret=None,
            facts=facts,
            keyring_backend=_keyring_backend(),
        )

    assert exc_info.value.reason_code == INTERACTIVE_INPUT_REQUIRED
    assert prompted == []


# --- Ref / SecretValue / redaction ---------------------------------------------


@pytest.mark.unit
def test_secret_value_repr_is_redacted() -> None:
    """Verify SecretValue never echoes the raw value via repr/str."""
    secret = SecretValue(_FAKE_OPENAI_TOKEN)

    assert _FAKE_OPENAI_TOKEN not in repr(secret)
    assert _FAKE_OPENAI_TOKEN not in str(secret)
    assert _FAKE_OPENAI_TOKEN not in f"{secret}"
    assert secret.reveal() == _FAKE_OPENAI_TOKEN


@pytest.mark.unit
@pytest.mark.parametrize(
    "token",
    [_FAKE_OPENAI_TOKEN, _FAKE_ANTHROPIC_TOKEN, _FAKE_GITHUB_TOKEN, _FAKE_PAT_TOKEN],
)
def test_credential_ref_never_serializes_raw_value(token: str) -> None:
    """Verify CredentialRef refuses to hold or serialize a token-shaped value."""
    with pytest.raises(HostSetupCredentialError) as exc_info:
        CredentialRef(backend="env_ref", key=token)

    assert exc_info.value.reason_code == CREDENTIAL_REF_INVALID
    assert token not in str(exc_info.value.to_dict())


@pytest.mark.unit
def test_ref_string_round_trips_through_provider_config() -> None:
    """Verify produced refs are accepted by ProviderConfig and parse back."""
    ref = CredentialRef(backend="keyring", key="awf/github")
    ref_string = ref.to_ref_string()

    assert ref_string == "keyring://awf/github"
    assert ProviderConfig(credential_ref=ref_string).credential_ref == ref_string

    recovered = CredentialRef.from_ref_string(ref_string)
    assert recovered.backend == "keyring"
    assert recovered.key == "awf/github"
    assert recovered == ref


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ref_string", "backend_name", "key"),
    [
        ("env://OPENAI_API_KEY", "env_ref", "OPENAI_API_KEY"),
        (
            "plain-file:///home/agent/.awf/github.secret",
            "plain_file",
            "/home/agent/.awf/github.secret",
        ),
    ],
)
def test_from_ref_string_parses_each_scheme(ref_string: str, backend_name: str, key: str) -> None:
    """Verify each supported scheme parses into the right backend and key."""
    ref = CredentialRef.from_ref_string(ref_string)

    assert ref.backend == backend_name
    assert ref.key == key
    assert ref.to_ref_string() == ref_string


@pytest.mark.unit
@pytest.mark.parametrize("bad_ref", ["literal-token-file", "ftp://example/secret", ""])
def test_from_ref_string_rejects_unknown_or_empty_refs(bad_ref: str) -> None:
    """Verify malformed or unknown-scheme refs are reason-coded."""
    with pytest.raises(HostSetupCredentialError) as exc_info:
        CredentialRef.from_ref_string(bad_ref)

    assert exc_info.value.reason_code == CREDENTIAL_REF_INVALID


@pytest.mark.unit
def test_raw_token_ref_is_rejected() -> None:
    """Verify a raw token ref is rejected by the parser and by config."""
    with pytest.raises(HostSetupCredentialError) as exc_info:
        CredentialRef.from_ref_string(_FAKE_GITHUB_TOKEN)

    assert exc_info.value.reason_code == CREDENTIAL_REF_INVALID
    assert _FAKE_GITHUB_TOKEN not in str(exc_info.value.to_dict())

    with pytest.raises(ValidationError):
        ProviderConfig(credential_ref=_FAKE_GITHUB_TOKEN)


@pytest.mark.unit
@pytest.mark.parametrize("token", [_FAKE_ANTHROPIC_TOKEN, _FAKE_GITHUB_TOKEN, _FAKE_PAT_TOKEN])
def test_logs_redact_token_shaped_inputs(token: str) -> None:
    """Verify rejected token-shaped refs are redacted in structured logs."""
    with structlog.testing.capture_logs() as captured, pytest.raises(HostSetupCredentialError):
        CredentialRef.from_ref_string(token)

    serialized = json.dumps(captured)
    assert token not in serialized
    assert "<redacted>" in serialized


# --- Host facts detection ------------------------------------------------------


@pytest.mark.unit
def test_detect_host_facts_uses_platform_and_display(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify host fact detection reads platform name and display env vars."""
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    headless = detect_host_facts(system="Linux", environ={})
    assert headless.is_linux is True
    assert headless.is_headless is True

    desktop = detect_host_facts(system="Linux", environ={"DISPLAY": ":0"})
    assert desktop.is_headless is False

    wayland = detect_host_facts(system="Linux", environ={"WAYLAND_DISPLAY": "wayland-0"})
    assert wayland.is_headless is False

    mac = detect_host_facts(system="Darwin", environ={})
    assert mac.is_linux is False


@pytest.mark.unit
def test_detect_host_facts_defaults_to_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify host fact detection falls back to the live platform/environment."""
    monkeypatch.setenv("DISPLAY", ":0")

    facts = detect_host_facts()
    assert isinstance(facts.os_name, str)
    assert facts.is_headless is False


# --- Backend availability ------------------------------------------------------


@pytest.mark.unit
def test_keyring_backend_availability_tracks_fail_backend() -> None:
    """Verify keyring availability flips with a fail backend or missing module."""
    facts = _make_host_facts()

    assert _keyring_backend(available=True).is_available(facts) is True
    assert _keyring_backend(available=False).is_available(facts) is False
    assert KeyringBackend(module_loader=lambda: None).is_available(facts) is False


@pytest.mark.unit
def test_keyring_backend_round_trips_through_fake_module() -> None:
    """Verify keyring backend stores and resolves through the injected module."""
    fake = FakeKeyring(available=True)
    backend = KeyringBackend(module_loader=lambda: fake)

    ref = backend.store("github", SecretValue(_FAKE_GITHUB_TOKEN))
    assert ref.to_ref_string() == "keyring://awf/github"
    assert fake.store[("awf", "github")] == _FAKE_GITHUB_TOKEN

    resolved = backend.resolve(ref)
    assert resolved is not None
    assert resolved.reveal() == _FAKE_GITHUB_TOKEN


@pytest.mark.unit
def test_keyring_backend_store_unavailable_is_reason_coded() -> None:
    """Verify storing through an unavailable keyring is reason-coded."""
    backend = KeyringBackend(module_loader=lambda: None)

    with pytest.raises(HostSetupCredentialError) as exc_info:
        backend.store("github", SecretValue(_FAKE_GITHUB_TOKEN))

    assert exc_info.value.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE
    assert backend.resolve(CredentialRef(backend="keyring", key="awf/github")) is None


@pytest.mark.unit
def test_keyring_backend_rejects_secret_shaped_logical_name() -> None:
    """Verify keyring storage rejects a token-shaped account name."""
    backend = _keyring_backend()

    with pytest.raises(HostSetupCredentialError) as exc_info:
        backend.store(_FAKE_GITHUB_TOKEN, SecretValue("value"))

    assert exc_info.value.reason_code == CREDENTIAL_REF_INVALID


@pytest.mark.unit
def test_env_ref_backend_is_always_available() -> None:
    """Verify the env-ref backend is available on every host."""
    assert EnvRefBackend().is_available(_make_host_facts(os_name="Windows", headless=False)) is True


@pytest.mark.unit
def test_plain_file_backend_availability_requires_headless_linux() -> None:
    """Verify plain-file availability is limited to headless Linux."""
    backend = PlainFileBackend()

    assert backend.is_available(_make_host_facts(os_name="Linux", headless=True)) is True
    assert backend.is_available(_make_host_facts(os_name="Linux", headless=False)) is False
    assert backend.is_available(_make_host_facts(os_name="Darwin", headless=True)) is False


@pytest.mark.unit
def test_plain_file_backend_store_without_dir_is_reason_coded() -> None:
    """Verify the plain-file backend refuses to store without a target dir."""
    with pytest.raises(HostSetupCredentialError) as exc_info:
        PlainFileBackend().store("github", SecretValue(_FAKE_GITHUB_TOKEN))

    assert exc_info.value.reason_code == CREDENTIAL_BACKEND_UNAVAILABLE


@pytest.mark.unit
def test_plain_file_backend_resolves_written_secret(tmp_path: Path) -> None:
    """Verify the plain-file backend resolves a previously written secret."""
    backend = PlainFileBackend(directory=tmp_path)
    ref = backend.store("github", SecretValue(_FAKE_GITHUB_TOKEN))

    resolved = backend.resolve(ref)
    assert resolved is not None
    assert resolved.reveal() == _FAKE_GITHUB_TOKEN
    missing = CredentialRef(backend="plain_file", key=str(tmp_path / "missing.secret"))
    assert backend.resolve(missing) is None


# --- Keyring module loader helpers ---------------------------------------------


@pytest.mark.unit
def test_keyring_module_loader_handles_missing_import() -> None:
    """Verify the keyring loader degrades to None on ImportError."""

    def _raise_import_error() -> object:
        """Simulate keyring being absent from the environment."""
        raise ImportError("No module named 'keyring'")

    assert credentials._load_keyring_module(importer=_raise_import_error) is None


@pytest.mark.unit
def test_keyring_module_loader_returns_injected_module() -> None:
    """Verify the keyring loader returns the imported module on success."""
    fake = FakeKeyring(available=True)

    assert credentials._load_keyring_module(importer=lambda: fake) is fake


@pytest.mark.unit
def test_default_keyring_module_loader_imports_real_keyring() -> None:
    """Verify the default loader imports the real keyring module."""
    module = credentials._load_keyring_module()

    assert module is not None
    assert hasattr(module, "get_password")


@pytest.mark.unit
def test_keyring_module_usable_handles_probe_errors() -> None:
    """Verify usability probing treats backend lookup errors as unusable."""

    class _ExplodingModule:
        """Keyring module whose backend lookup raises a runtime error."""

        def get_keyring(self) -> object:
            """Raise to simulate a misconfigured keyring backend."""
            raise RuntimeError("backend init failed")

    assert credentials._keyring_module_usable(_ExplodingModule()) is False
    assert credentials._keyring_module_usable(FakeKeyring(available=True)) is True


# --- resolve seam --------------------------------------------------------------


@pytest.mark.unit
def test_resolve_credential_ref_dispatches_per_scheme(tmp_path: Path) -> None:
    """Verify the resolve seam dispatches env, keyring, and plain-file refs."""
    env_value = resolve_credential_ref(
        "env://OPENAI_API_KEY",
        environ={"OPENAI_API_KEY": _FAKE_OPENAI_TOKEN},
    )
    assert env_value is not None
    assert env_value.reveal() == _FAKE_OPENAI_TOKEN

    fake = FakeKeyring(available=True)
    fake.set_password("awf", "github", _FAKE_GITHUB_TOKEN)
    keyring_value = resolve_credential_ref(
        "keyring://awf/github",
        keyring_module_loader=lambda: fake,
    )
    assert keyring_value is not None
    assert keyring_value.reveal() == _FAKE_GITHUB_TOKEN

    secret_path = tmp_path / "github.secret"
    secret_path.write_text(_FAKE_GITHUB_TOKEN, encoding="utf-8")
    plain_value = resolve_credential_ref(f"plain-file://{secret_path}")
    assert plain_value is not None
    assert plain_value.reveal() == _FAKE_GITHUB_TOKEN


# --- config persistence --------------------------------------------------------


@pytest.mark.unit
def test_set_provider_credential_ref_rebuilds_providers_mapping() -> None:
    """Verify setting a provider ref rebuilds the frozen providers mapping."""
    base = HostSetupConfig(
        providers={"openai": ProviderConfig(credential_ref="env://OPENAI_API_KEY")}
    )

    updated = set_provider_credential_ref(
        base,
        provider="github",
        credential_ref="keyring://awf/github",
        source="keyring",
        status="ready",
    )

    assert get_provider_credential_ref(updated, "github") == "keyring://awf/github"
    assert get_provider_credential_ref(updated, "openai") == "env://OPENAI_API_KEY"
    assert get_provider_credential_ref(base, "github") is None
    assert isinstance(updated.providers, Mapping)


@pytest.mark.unit
def test_set_provider_credential_ref_rejects_raw_secret() -> None:
    """Verify setting a raw-token ref is rejected before persistence."""
    base = HostSetupConfig()

    with pytest.raises(ValidationError):
        set_provider_credential_ref(base, provider="github", credential_ref=_FAKE_GITHUB_TOKEN)


@pytest.mark.unit
def test_credential_ref_persisted_in_host_setup_config(tmp_path: Path) -> None:
    """Verify credential refs round-trip through config without raw values."""
    config_path = tmp_path / ".awf" / "config.yml"
    facts = _make_host_facts()
    fake = FakeKeyring(available=True)

    ref = store_provider_credential(
        logical_name="github",
        secret=SecretValue(_FAKE_GITHUB_TOKEN),
        facts=facts,
        keyring_backend=KeyringBackend(module_loader=lambda: fake),
    )
    config = set_provider_credential_ref(
        HostSetupConfig(),
        provider="github",
        credential_ref=ref.to_ref_string(),
        source="keyring",
    )

    write_host_setup_config(config, path=config_path)

    raw_text = config_path.read_text(encoding="utf-8")
    assert _FAKE_GITHUB_TOKEN not in raw_text
    assert "keyring://awf/github" in raw_text
    if os.name == "posix":
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    loaded = read_host_setup_config(path=config_path)
    assert get_provider_credential_ref(loaded, "github") == "keyring://awf/github"


@pytest.mark.unit
def test_get_provider_credential_ref_handles_missing_provider() -> None:
    """Verify the ref getter returns None for unknown providers."""
    assert get_provider_credential_ref(HostSetupConfig(), "missing") is None


# --- exports -------------------------------------------------------------------


@pytest.mark.unit
def test_public_credential_symbols_are_exported() -> None:
    """Verify the new credential symbols are re-exported from the package."""
    for symbol in (
        "CredentialRef",
        "SecretValue",
        "HostFacts",
        "detect_host_facts",
        "KeyringBackend",
        "EnvRefBackend",
        "PlainFileBackend",
        "select_credential_backend",
        "offered_credential_backends",
        "store_provider_credential",
        "resolve_credential_ref",
        "HostSetupCredentialError",
        "set_provider_credential_ref",
        "get_provider_credential_ref",
    ):
        assert hasattr(host_setup, symbol)
        assert symbol in host_setup.__all__
