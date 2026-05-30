"""Credential backend abstraction for first-run host setup.

Turns a provided provider secret into a stored *reference*
(``keyring://`` / ``env://`` / ``plain-file://``) recorded on
``ProviderConfig.credential_ref`` — never the raw value. Three backends are
supported:

- ``keyring`` — OS keychain, the default when available.
- ``env_ref`` — records only the environment variable name.
- ``plain_file`` — explicit, gated opt-in; Linux + headless only (H02).

This module owns credential *storage policy and ref shaping only*. Provider
orchestration (T07), support-bundle hardening (T17), and the setup CLI surface
(T04) live elsewhere; ``store_provider_credential`` / ``resolve_credential_ref``
are the minimal seams those layers consume.
"""

from __future__ import annotations

import os
import platform
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, cast, runtime_checkable

from pydantic import ValidationError

from awf.common.logging import get_logger
from awf.common.redaction import redact_secrets
from awf.host_setup.config import (
    HostSetupConfig,
    ProviderConfig,
    _looks_like_secret_value,
)
from awf.host_setup.rendering import (
    CREDENTIAL_BACKEND_UNAVAILABLE,
    CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED,
    CREDENTIAL_REF_INVALID,
    INTERACTIVE_INPUT_REQUIRED,
)

_LOGGER = get_logger(__name__)

BackendName = Literal["keyring", "env_ref", "plain_file"]

DEFAULT_KEYRING_SERVICE = "awf"

# Scheme prefixes accepted by ``ProviderConfig._validate_credential_ref``.
_SCHEME_BY_BACKEND: dict[BackendName, str] = {
    "keyring": "keyring://",
    "env_ref": "env://",
    "plain_file": "plain-file://",
}

_ENV_VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_LOGICAL_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_SECRET_VALUE_PLACEHOLDER = "<secret>"


class HostSetupCredentialError(RuntimeError):
    """Reason-coded credential backend failure with secret-free details."""

    def __init__(
        self,
        *,
        reason_code: str,
        message: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        """Build a reason-coded credential error without embedding secrets."""
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable, secret-free diagnostic payload."""
        payload: dict[str, object] = {
            "status": "failed",
            "reason_code": self.reason_code,
            "message": self.message,
        }
        if self.details:
            payload["details"] = self.details
        return payload


class SecretValue:
    """A raw secret wrapper whose ``repr``/``str`` never reveal the value.

    Only ``reveal()`` returns the underlying string. This keeps secrets out of
    f-strings, log payloads, and exception text by construction.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        """Wrap a raw secret value behind a redacted representation."""
        self._value = value

    def reveal(self) -> str:
        """Return the raw secret value (the only raw accessor)."""
        return self._value

    def __repr__(self) -> str:
        """Return a redacted representation safe for logs and tracebacks."""
        return f"SecretValue({_SECRET_VALUE_PLACEHOLDER!r})"

    def __str__(self) -> str:
        """Return a redacted placeholder instead of the raw value."""
        return _SECRET_VALUE_PLACEHOLDER


@dataclass(frozen=True)
class HostFacts:
    """Injectable host facts driving credential backend platform branches."""

    os_name: str
    is_linux: bool
    is_headless: bool


def detect_host_facts(
    *,
    system: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> HostFacts:
    """Detect host platform/headless facts; inputs are injectable for tests."""
    os_name = platform.system() if system is None else system
    env = os.environ if environ is None else environ
    is_headless = not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))
    return HostFacts(os_name=os_name, is_linux=os_name == "Linux", is_headless=is_headless)


@dataclass(frozen=True)
class CredentialRef:
    """In-memory credential reference; never carries the secret value."""

    backend: BackendName
    key: str
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Reject empty or secret-shaped keys and freeze metadata as a dict."""
        if not self.key or _looks_like_secret_value(self.key):
            raise _credential_ref_invalid_error(issue="secret_shaped_key")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_ref_string(self) -> str:
        """Emit the scheme-prefixed ref string accepted by ``ProviderConfig``."""
        ref = f"{_SCHEME_BY_BACKEND[self.backend]}{self.key}"
        _validate_ref_string(ref, candidate=ref)
        return ref

    @classmethod
    def from_ref_string(cls, value: str) -> CredentialRef:
        """Parse a stored ref string into a ``CredentialRef``."""
        _validate_ref_string(value, candidate=value)
        for backend, scheme in _SCHEME_BY_BACKEND.items():
            if value.startswith(scheme):
                return cls(backend=backend, key=value[len(scheme) :])
        # ``_validate_ref_string`` accepts only the known schemes, so the loop
        # above always returns first; this guard is defensive only.
        raise _credential_ref_invalid_error(issue="unknown_scheme")  # pragma: no cover


@runtime_checkable
class CredentialBackend(Protocol):
    """Contract every credential backend implements."""

    name: BackendName
    requires_secret_value: bool

    def is_available(self, facts: HostFacts) -> bool:
        """Return whether this backend can be used on the given host."""

    def store(self, logical_name: str, secret: SecretValue) -> CredentialRef:
        """Persist the secret behind the backend and return its reference."""

    def resolve(self, ref: CredentialRef) -> SecretValue | None:
        """Return the secret for a stored reference, or ``None`` if absent."""


class _KeyringModule(Protocol):
    """Subset of the ``keyring`` module API this backend depends on."""

    def get_keyring(self) -> object:
        """Return the active keyring backend implementation."""

    def get_password(self, service: str, username: str) -> str | None:
        """Return a stored password, or ``None`` if absent."""

    def set_password(self, service: str, username: str, password: str) -> None:
        """Store a password in the active keyring backend."""


def _import_keyring() -> _KeyringModule:
    """Import the real ``keyring`` module lazily (kept import-safe)."""
    import keyring

    return cast(_KeyringModule, keyring)


def _load_keyring_module(
    *,
    importer: Callable[[], _KeyringModule] | None = None,
) -> _KeyringModule | None:
    """Return the keyring module, or ``None`` when it cannot be imported."""
    load = importer or _import_keyring
    try:
        return load()
    except ImportError:
        return None


def _is_fail_keyring_backend(backend: object) -> bool:
    """Return whether a keyring backend is the no-op ``fail`` backend."""
    return type(backend).__module__.startswith("keyring.backends.fail")


def _keyring_module_usable(module: _KeyringModule) -> bool:
    """Return whether the keyring module has a usable (non-fail) backend."""
    try:
        backend = module.get_keyring()
    except (ImportError, RuntimeError, OSError):
        return False
    return not _is_fail_keyring_backend(backend)


class KeyringBackend:
    """OS keychain backend; lazy ``keyring`` import keeps core import-safe."""

    name: BackendName = "keyring"
    requires_secret_value: bool = True

    def __init__(
        self,
        *,
        service: str = DEFAULT_KEYRING_SERVICE,
        module_loader: Callable[[], _KeyringModule | None] | None = None,
    ) -> None:
        """Build a keyring backend with an injectable module loader."""
        self._service = service
        self._module_loader = module_loader or _load_keyring_module

    def is_available(self, facts: HostFacts) -> bool:
        """Return whether an importable, usable keychain backend exists."""
        del facts  # keychain availability is host-platform independent here
        module = self._module_loader()
        return module is not None and _keyring_module_usable(module)

    def store(self, logical_name: str, secret: SecretValue) -> CredentialRef:
        """Store the secret in the keychain and return a ``keyring://`` ref."""
        module = self._module_loader()
        if module is None or not _keyring_module_usable(module):
            raise _credential_backend_unavailable_error(offered=())
        account = _validate_logical_name(logical_name)
        module.set_password(self._service, account, secret.reveal())
        return CredentialRef(backend="keyring", key=f"{self._service}/{account}")

    def resolve(self, ref: CredentialRef) -> SecretValue | None:
        """Resolve a ``keyring://service/account`` ref from the keychain."""
        module = self._module_loader()
        if module is None:
            return None
        service, _, account = ref.key.partition("/")
        raw = module.get_password(service, account)
        return SecretValue(raw) if raw is not None else None


class EnvRefBackend:
    """Environment-variable reference backend; records only the var name."""

    name: BackendName = "env_ref"
    requires_secret_value: bool = False

    def __init__(self, *, environ: Mapping[str, str] | None = None) -> None:
        """Build an env-ref backend with an injectable environment mapping."""
        self._environ = environ

    def is_available(self, facts: HostFacts) -> bool:
        """Return ``True``; env refs work on every host."""
        del facts
        return True

    def store(self, logical_name: str, secret: SecretValue) -> CredentialRef:
        """Record only the environment variable name as an ``env://`` ref."""
        del secret  # env refs never read or persist the value
        var_name = logical_name.strip()
        if not _ENV_VAR_NAME_RE.match(var_name):
            raise _credential_ref_invalid_error(issue="env_var_name")
        return CredentialRef(backend="env_ref", key=var_name)

    def resolve(self, ref: CredentialRef) -> SecretValue | None:
        """Resolve the value from the environment by variable name."""
        env = os.environ if self._environ is None else self._environ
        raw = env.get(ref.key)
        return SecretValue(raw) if raw is not None else None


class PlainFileBackend:
    """Opt-in plain-file backend; available only on headless Linux (H02)."""

    name: BackendName = "plain_file"
    requires_secret_value: bool = True

    def __init__(self, *, directory: Path | None = None) -> None:
        """Build a plain-file backend writing under ``directory`` (for store)."""
        self._directory = directory

    def is_available(self, facts: HostFacts) -> bool:
        """Return whether plain-file storage is host-permitted (headless Linux)."""
        return facts.is_linux and facts.is_headless

    def store(self, logical_name: str, secret: SecretValue) -> CredentialRef:
        """Write the secret to a ``0600`` file and return a ``plain-file://`` ref."""
        if self._directory is None:
            raise _credential_backend_unavailable_error(offered=())
        account = _validate_logical_name(logical_name)
        self._directory.mkdir(parents=True, exist_ok=True)
        _chmod_best_effort(self._directory, 0o700)
        target = (self._directory / f"{account}.secret").resolve()
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(secret.reveal())
        _chmod_best_effort(target, 0o600)
        return CredentialRef(backend="plain_file", key=str(target), metadata={"mode": "0600"})

    def resolve(self, ref: CredentialRef) -> SecretValue | None:
        """Read a previously written plain-file secret, or ``None`` if absent."""
        try:
            raw = Path(ref.key).read_text(encoding="utf-8")
        except OSError:
            return None
        return SecretValue(raw)


def offered_credential_backends(
    facts: HostFacts,
    *,
    allow_plain_secrets: bool,
    plain_file_consent: bool,
) -> tuple[BackendName, ...]:
    """Return the non-keyring fallback backends a host may explicitly choose."""
    offered: list[BackendName] = ["env_ref"]
    if _plain_file_permitted(
        facts,
        allow_plain_secrets=allow_plain_secrets,
        plain_file_consent=plain_file_consent,
    ):
        offered.append("plain_file")
    return tuple(offered)


def select_credential_backend(
    *,
    facts: HostFacts,
    requested: BackendName | None = None,
    secret_available: bool = True,
    allow_plain_secrets: bool = False,
    plain_file_consent: bool = False,
    keyring_backend: CredentialBackend | None = None,
    plain_file_dir: Path | None = None,
) -> CredentialBackend:
    """Resolve the credential backend to use, applying default + fallback policy.

    Keyring is the default when available. With no explicit ``requested`` backend
    and no usable keychain, the caller must pick a fallback explicitly, so this
    raises ``CREDENTIAL_BACKEND_UNAVAILABLE`` with the offered set. Plain-file
    selection is gated on allow + consent + headless Linux. When the chosen
    backend needs a value and none was supplied, this raises
    ``INTERACTIVE_INPUT_REQUIRED`` rather than prompting.
    """
    keyring = keyring_backend or KeyringBackend()
    offered = offered_credential_backends(
        facts,
        allow_plain_secrets=allow_plain_secrets,
        plain_file_consent=plain_file_consent,
    )

    backend = _resolve_requested_backend(
        facts=facts,
        requested=requested,
        keyring=keyring,
        offered=offered,
        allow_plain_secrets=allow_plain_secrets,
        plain_file_consent=plain_file_consent,
        plain_file_dir=plain_file_dir,
    )

    if backend.requires_secret_value and not secret_available:
        raise _interactive_input_required_error(backend=backend.name)
    return backend


def _resolve_requested_backend(
    *,
    facts: HostFacts,
    requested: BackendName | None,
    keyring: CredentialBackend,
    offered: tuple[BackendName, ...],
    allow_plain_secrets: bool,
    plain_file_consent: bool,
    plain_file_dir: Path | None,
) -> CredentialBackend:
    """Pick the concrete backend for a request, enforcing keyring + plain gates."""
    if requested is None or requested == "keyring":
        if keyring.is_available(facts):
            return keyring
        # No usable keychain and no explicit fallback chosen: the caller must
        # pick one of the offered fallbacks (env ref / gated plain file).
        raise _credential_backend_unavailable_error(offered=offered)
    if requested == "env_ref":
        return EnvRefBackend()
    _assert_plain_file_permitted(
        facts,
        allow_plain_secrets=allow_plain_secrets,
        plain_file_consent=plain_file_consent,
    )
    if plain_file_dir is None:
        raise _credential_backend_unavailable_error(offered=offered)
    return PlainFileBackend(directory=plain_file_dir)


def store_provider_credential(
    *,
    logical_name: str,
    secret: SecretValue | None,
    facts: HostFacts,
    requested_backend: BackendName | None = None,
    allow_plain_secrets: bool = False,
    plain_file_consent: bool = False,
    keyring_backend: CredentialBackend | None = None,
    plain_file_dir: Path | None = None,
) -> CredentialRef:
    """Select a backend and store ``secret`` behind it, returning its reference.

    Minimal store seam for provider setup (T07). Enforces non-interactive
    behavior: a value-requiring backend with no secret raises
    ``INTERACTIVE_INPUT_REQUIRED`` instead of prompting.
    """
    backend = select_credential_backend(
        facts=facts,
        requested=requested_backend,
        secret_available=secret is not None,
        allow_plain_secrets=allow_plain_secrets,
        plain_file_consent=plain_file_consent,
        keyring_backend=keyring_backend,
        plain_file_dir=plain_file_dir,
    )
    ref = backend.store(logical_name, secret if secret is not None else SecretValue(""))
    _LOGGER.info(
        "host_setup.credential_stored",
        backend=ref.backend,
        credential_ref=ref.to_ref_string(),
    )
    return ref


def resolve_credential_ref(
    ref_string: str,
    *,
    environ: Mapping[str, str] | None = None,
    keyring_module_loader: Callable[[], _KeyringModule | None] | None = None,
) -> SecretValue | None:
    """Resolve a stored credential ref string back into its secret value.

    Minimal resolve seam for provider setup (T07); dispatches per ref scheme.
    """
    ref = CredentialRef.from_ref_string(ref_string)
    if ref.backend == "env_ref":
        return EnvRefBackend(environ=environ).resolve(ref)
    if ref.backend == "keyring":
        return KeyringBackend(module_loader=keyring_module_loader).resolve(ref)
    return PlainFileBackend().resolve(ref)


def set_provider_credential_ref(
    config: HostSetupConfig,
    *,
    provider: str,
    credential_ref: str,
    source: str | None = None,
    status: str = "ready",
) -> HostSetupConfig:
    """Return a copy of ``config`` with ``provider``'s credential ref recorded.

    Rebuilds the frozen providers mapping rather than mutating it in place. The
    ``credential_ref`` is validated by ``ProviderConfig`` — a raw secret value
    raises before any persistence.
    """
    providers: dict[str, ProviderConfig] = dict(config.providers)
    existing = providers.get(provider)
    providers[provider] = ProviderConfig(
        credential_ref=credential_ref,
        source=source if source is not None else (existing.source if existing else None),
        status=status,
    )
    return config.model_copy(update={"providers": providers})


def get_provider_credential_ref(config: HostSetupConfig, provider: str) -> str | None:
    """Return the recorded credential ref for ``provider``, or ``None``."""
    provider_config = config.providers.get(provider)
    return provider_config.credential_ref if provider_config is not None else None


def _plain_file_permitted(
    facts: HostFacts,
    *,
    allow_plain_secrets: bool,
    plain_file_consent: bool,
) -> bool:
    """Return whether plain-file storage is fully permitted (host + consent)."""
    return allow_plain_secrets and plain_file_consent and facts.is_linux and facts.is_headless


def _assert_plain_file_permitted(
    facts: HostFacts,
    *,
    allow_plain_secrets: bool,
    plain_file_consent: bool,
) -> None:
    """Raise ``CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED`` unless fully permitted."""
    if not allow_plain_secrets or not plain_file_consent:
        raise _plain_file_consent_required_error(details={"requirement": "allow_and_consent"})
    if not (facts.is_linux and facts.is_headless):
        raise _plain_file_consent_required_error(
            details={
                "requirement": "headless_linux",
                "os_name": facts.os_name,
                "is_headless": facts.is_headless,
            }
        )


def _validate_logical_name(logical_name: str) -> str:
    """Validate a provider/account name used in keyring and plain-file refs."""
    name = logical_name.strip()
    if _looks_like_secret_value(name) or not _LOGICAL_NAME_RE.match(name):
        raise _credential_ref_invalid_error(issue="logical_name")
    return name


def _validate_ref_string(value: str, *, candidate: str) -> None:
    """Validate a ref string against the existing ``ProviderConfig`` contract."""
    try:
        ProviderConfig(credential_ref=value)
    except ValidationError as exc:
        _LOGGER.warning(
            "host_setup.credential_ref_rejected",
            candidate=redact_secrets(candidate),
        )
        raise _credential_ref_invalid_error(issue="invalid_ref") from exc


def _chmod_best_effort(path: Path, mode: int) -> None:
    """Apply POSIX permissions when supported by the host platform."""
    if os.name != "posix":  # pragma: no cover - exercised only on non-POSIX hosts
        return
    try:
        path.chmod(mode)
    except OSError:  # pragma: no cover - best-effort hardening only
        return


def _interactive_input_required_error(*, backend: BackendName) -> HostSetupCredentialError:
    """Build a non-interactive secret-input failure."""
    return HostSetupCredentialError(
        reason_code=INTERACTIVE_INPUT_REQUIRED,
        message="AWF setup needs interactive input that is unavailable in this run.",
        details={"backend": backend},
    )


def _credential_backend_unavailable_error(
    *,
    offered: tuple[BackendName, ...],
) -> HostSetupCredentialError:
    """Build a no-usable-backend failure carrying the offered fallback set."""
    return HostSetupCredentialError(
        reason_code=CREDENTIAL_BACKEND_UNAVAILABLE,
        message="AWF could not access a usable credential storage backend.",
        details={"offered_backends": list(offered)},
    )


def _plain_file_consent_required_error(
    *,
    details: Mapping[str, object],
) -> HostSetupCredentialError:
    """Build a plain-file consent/host gate failure."""
    return HostSetupCredentialError(
        reason_code=CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED,
        message=(
            "AWF refused to store a provider credential in a plain file "
            "without explicit consent on an approved headless Linux path."
        ),
        details=details,
    )


def _credential_ref_invalid_error(*, issue: str) -> HostSetupCredentialError:
    """Build a malformed-credential-reference failure (no secret in details)."""
    return HostSetupCredentialError(
        reason_code=CREDENTIAL_REF_INVALID,
        message="AWF rejected a malformed provider credential reference.",
        details={"issue": issue},
    )


__all__ = [
    "DEFAULT_KEYRING_SERVICE",
    "BackendName",
    "CredentialBackend",
    "CredentialRef",
    "EnvRefBackend",
    "HostFacts",
    "HostSetupCredentialError",
    "KeyringBackend",
    "PlainFileBackend",
    "SecretValue",
    "detect_host_facts",
    "get_provider_credential_ref",
    "offered_credential_backends",
    "resolve_credential_ref",
    "select_credential_backend",
    "set_provider_credential_ref",
    "store_provider_credential",
]
