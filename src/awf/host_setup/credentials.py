"""Credential backend abstraction for AWF first-run host setup.

This module produces safe credential *references* (``keyring://``, ``env://``,
``plain-file://``) plus non-secret backend metadata. It never returns, logs, or
prints raw secret values, and it never prompts: missing required input in a
non-interactive run fails with ``INTERACTIVE_INPUT_REQUIRED``.

The keyring backend uses a lazy, optional import and dependency injection, so
the module and its tests work whether or not the ``keyring`` library is
installed. Tests inject fakes and never touch a real OS keychain. Credential
*resolution* (reading values back) and interactive prompting belong to the
setup/provider CLI flows (T04/T07), not here.
"""

from __future__ import annotations

import importlib
import os
import platform
import re
import secrets
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from awf.common.audit import redact_audit_text
from awf.common.token_patterns import compile_known_token_re
from awf.host_setup.rendering import (
    CREDENTIAL_BACKEND_UNAVAILABLE,
    CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED,
    CREDENTIAL_REF_INVALID,
    INTERACTIVE_INPUT_REQUIRED,
)

CredentialBackendKind = Literal["keyring", "env_ref", "plain_file"]
CREDENTIAL_BACKENDS: tuple[CredentialBackendKind, ...] = (
    "keyring",
    "env_ref",
    "plain_file",
)

_BACKEND_REF_PREFIXES: Mapping[CredentialBackendKind, str] = {
    "keyring": "keyring://",
    "env_ref": "env://",
    "plain_file": "plain-file://",
}

_KEYRING_SERVICE_PREFIX = "awf"
_DEFAULT_ACCOUNT = "default"
_DEFAULT_SECRETS_DIR = "~/.awf/secrets"

# A produced ref is ultimately persisted to ``ProviderConfig.credential_ref``,
# so it must never exceed that field's ``max_length``. Provider/account/path
# identifiers are otherwise unbounded, so a long one could mint a ref that passes
# ``CredentialRef`` validation yet cannot be stored. Keep this cap in lockstep
# with ``awf.host_setup.config.ProviderConfig.credential_ref`` (config must not
# import this module to avoid a cycle; a test guards the two against drift).
MAX_CREDENTIAL_REF_LENGTH = 512

# Env var names follow POSIX-ish uppercase identifiers (e.g. ``OPENAI_API_KEY``,
# ``GH_TOKEN``); provider/account identifiers stay filesystem- and ref-safe.
_ENV_VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
# Reuse the shared token recognizer so token-shaped inputs are rejected/redacted
# the same way as everywhere else in AWF.
_TOKEN_SHAPE_RE = compile_known_token_re(
    ignorecase=True,
    match_truncated_provider_tokens=True,
)
# ``keyring`` resolves to these no-op backends on hosts without a usable
# keychain (e.g. headless Linux); treat them as unavailable.
_NOOP_KEYRING_BACKEND_LEAVES = frozenset({"fail", "null"})


class CredentialError(RuntimeError):
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


class CredentialRef(BaseModel):
    """A safe provider credential reference and the backend that produced it."""

    # ``hide_input_in_errors`` mirrors ``config._HostSetupBaseModel``: a ref that
    # accidentally embeds a raw secret is rejected by ``_validate_ref``, and this
    # flag stops Pydantic echoing that input into ``str(ValidationError)`` (which
    # ``logger.exception`` formats), preventing a secret leak.
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        hide_input_in_errors=True,
    )

    backend: CredentialBackendKind
    ref: str = Field(min_length=1, max_length=MAX_CREDENTIAL_REF_LENGTH)

    @model_validator(mode="after")
    def _validate_ref(self) -> CredentialRef:
        """Require the ref to match the backend scheme and carry no raw secret."""
        prefix = _BACKEND_REF_PREFIXES[self.backend]
        if not self.ref.startswith(prefix):
            raise ValueError(f"ref for backend {self.backend!r} must start with {prefix!r}")
        if _looks_like_secret(self.ref):
            raise ValueError("ref must not contain a raw secret value")
        return self

    def to_provider_config_fields(self, *, status: str = "ready") -> dict[str, str]:
        """Return non-secret fields to build or update a ``ProviderConfig``."""
        return {"credential_ref": self.ref, "backend": self.backend, "status": status}


class HostCredentialCapabilities(BaseModel):
    """Host facts that gate which credential backends are usable."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    os_name: str = Field(min_length=1, max_length=128)
    is_headless: bool

    @property
    def supports_plain_file(self) -> bool:
        """Return whether plain-file storage is permitted (headless Linux only)."""
        return self.os_name == "Linux" and self.is_headless


def detect_host_credential_capabilities(
    *,
    system: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> HostCredentialCapabilities:
    """Derive credential capabilities from the platform and environment.

    ``system`` and ``environ`` are injectable for deterministic tests. A host is
    "headless" only on Linux with neither ``DISPLAY`` nor ``WAYLAND_DISPLAY``;
    non-Linux hosts are treated as non-headless so plain-file stays disabled.
    """
    resolved_system = (system if system is not None else platform.system()) or "unknown"
    resolved_environ = os.environ if environ is None else environ
    if resolved_system == "Linux":
        is_headless = not (
            resolved_environ.get("DISPLAY") or resolved_environ.get("WAYLAND_DISPLAY")
        )
    else:
        is_headless = False
    return HostCredentialCapabilities(os_name=resolved_system, is_headless=is_headless)


@dataclass(frozen=True)
class CredentialRequest:
    """Inputs for producing one provider credential reference.

    ``secret_source`` is a lazy callable so a secret value is only pulled when a
    backend actually needs it (keyring/plain_file); env_ref never pulls a value.

    ``non_interactive`` is advisory metadata only: this module never prompts, so
    missing required input always raises ``INTERACTIVE_INPUT_REQUIRED`` regardless
    of its value. It is surfaced solely as a secret-free detail on that error to
    record the run's intent; it does not gate any prompting or fallback path.
    """

    provider: str
    account: str = _DEFAULT_ACCOUNT
    env_var: str | None = None
    secret_source: Callable[[], str | None] | None = None
    # Advisory metadata only — does NOT gate behavior (see the class docstring);
    # the module is unconditionally non-interactive.
    non_interactive: bool = True


class KeyringModule(Protocol):
    """Structural surface of the optional ``keyring`` library used here."""

    def get_keyring(self) -> object:
        """Return the resolved keyring backend instance."""

    def set_password(self, service: str, username: str, password: str) -> None:
        """Store a secret under ``service``/``username`` in the OS keychain."""


class CredentialBackend(Protocol):
    """One credential backend that yields a safe reference."""

    kind: str

    def is_available(self) -> bool:
        """Return whether this backend can store a credential on this host."""

    def create_ref(self, request: CredentialRequest) -> CredentialRef:
        """Store/encode the credential and return a safe reference."""


class KeyringCredentialBackend:
    """Store secrets in the OS keychain via the optional ``keyring`` library."""

    kind = "keyring"

    def __init__(
        self,
        *,
        keyring_module: KeyringModule | None = None,
        service_prefix: str = _KEYRING_SERVICE_PREFIX,
    ) -> None:
        """Build a keyring backend, optionally injecting a keyring module."""
        self._keyring_module = keyring_module
        self._service_prefix = service_prefix

    def _module(self) -> KeyringModule | None:
        """Return the injected module or lazily import the real ``keyring``."""
        if self._keyring_module is not None:
            return self._keyring_module
        return _import_keyring_module()

    def is_available(self) -> bool:
        """Return whether a usable (non no-op) keyring backend is resolvable."""
        module = self._module()
        if module is None:
            return False
        return _keyring_module_has_usable_backend(module)

    def create_ref(self, request: CredentialRequest) -> CredentialRef:
        """Store the secret in the keychain and return a ``keyring://`` ref."""
        module = self._module()
        if module is None:
            raise CredentialError(
                reason_code=CREDENTIAL_BACKEND_UNAVAILABLE,
                message="No keyring backend is available on this host.",
                details={"backend": self.kind},
            )
        provider = _require_safe_identifier(request.provider, field="provider")
        account = _require_safe_identifier(request.account, field="account")
        service = f"{self._service_prefix}/{provider}"
        # Validate the ref before the keychain write so an over-long identifier
        # never strands a secret the resulting ref could not be stored against.
        ref = _build_credential_ref("keyring", f"keyring://{service}/{account}")
        secret = _pull_secret(request)
        try:
            module.set_password(service, account, secret)
        except Exception as exc:
            # The resolved keyring backend is an unbounded third-party surface
            # (SecretService/DBus, macOS Keychain, KWallet, ...): besides
            # ``keyring.errors.KeyringError`` it can raise standard exceptions
            # such as ``OSError`` (DBus unavailable) or a bare ``RuntimeError``
            # from a half-configured stack. Translate every non-fatal failure
            # into a reason-coded ``CredentialError`` so callers degrade to
            # env-ref instead of crashing; ``BaseException`` (KeyboardInterrupt,
            # SystemExit, CancelledError) still propagates.
            raise CredentialError(
                reason_code=CREDENTIAL_BACKEND_UNAVAILABLE,
                message="The keyring backend rejected the credential write.",
                details={"backend": self.kind, "error_type": type(exc).__name__},
            ) from exc
        return ref


class EnvRefCredentialBackend:
    """Encode an environment-variable name; never stores a value."""

    kind = "env_ref"

    def is_available(self) -> bool:
        """Return ``True`` — a name pointer needs no storage backend."""
        return True

    def create_ref(self, request: CredentialRequest) -> CredentialRef:
        """Validate the env var name and return an ``env://NAME`` ref."""
        env_var = request.env_var
        # A whitespace-only name strips to ``""`` and is semantically "no name
        # provided", so treat it as missing input rather than letting the empty
        # post-strip identifier fall through to ``CREDENTIAL_REF_INVALID``.
        if not env_var or not env_var.strip():
            raise _interactive_input_required(request, missing="env_var")
        name = env_var.strip()
        if _TOKEN_SHAPE_RE.search(name) is not None:
            raise CredentialError(
                reason_code=CREDENTIAL_REF_INVALID,
                message="Environment variable name resembles a raw secret value.",
                details={"field": "env_var"},
            )
        if not _ENV_VAR_NAME_RE.fullmatch(name):
            raise CredentialError(
                reason_code=CREDENTIAL_REF_INVALID,
                message="Environment variable name is not a valid identifier.",
                details={"field": "env_var"},
            )
        return _build_credential_ref("env_ref", f"env://{name}")


class PlainFileCredentialBackend:
    """Opt-in fallback that writes a ``0600`` secret file on headless Linux."""

    kind = "plain_file"

    def __init__(
        self,
        *,
        capabilities: HostCredentialCapabilities,
        allow_plain_secrets: bool,
        consent: bool,
        secrets_dir: str | Path | None = None,
    ) -> None:
        """Build a plain-file backend gated by capabilities, flag, and consent."""
        self._capabilities = capabilities
        self._allow_plain_secrets = allow_plain_secrets
        self._consent = consent
        self._secrets_dir = (
            Path(secrets_dir).resolve()
            if secrets_dir is not None
            else _default_secrets_dir().resolve()
        )

    def is_available(self) -> bool:
        """Return whether flag, consent, and a headless-Linux host all hold."""
        return (
            self._allow_plain_secrets and self._consent and self._capabilities.supports_plain_file
        )

    def create_ref(self, request: CredentialRequest) -> CredentialRef:
        """Write the secret to a ``0600`` file and return a ``plain-file://`` ref."""
        if not (self._allow_plain_secrets and self._consent):
            raise CredentialError(
                reason_code=CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED,
                message=(
                    "Plain-file credential storage requires --allow-plain-secrets "
                    "and recorded consent."
                ),
                details={
                    "allow_plain_secrets": self._allow_plain_secrets,
                    "consent": self._consent,
                },
            )
        if not self._capabilities.supports_plain_file:
            raise CredentialError(
                reason_code=CREDENTIAL_BACKEND_UNAVAILABLE,
                message="Plain-file credential storage is limited to headless Linux hosts.",
                details={
                    "os_name": self._capabilities.os_name,
                    "is_headless": self._capabilities.is_headless,
                },
            )
        provider = _require_safe_identifier(request.provider, field="provider")
        target = self._secrets_dir / provider
        # Validate the ref before writing so an over-long path never leaves an
        # orphaned secret file the resulting ref could not be stored against.
        ref = _build_credential_ref("plain_file", f"plain-file://{target}")
        secret = _pull_secret(request)
        _write_secret_file(target, secret)
        return ref


def select_credential_backend(
    *,
    preferred: str | None,
    capabilities: HostCredentialCapabilities,
    allow_plain_secrets: bool,
    plain_file_consent: bool,
    keyring_backend: CredentialBackend | None = None,
    env_backend: CredentialBackend | None = None,
    plain_file_backend: CredentialBackend | None = None,
) -> CredentialBackend:
    """Resolve the credential backend for a preference, defaulting to keyring.

    ``preferred`` ``None``/``"keyring"`` selects keyring when available and falls
    back to env_ref otherwise (the safe default; headless-Linux-no-keychain gets
    the env-ref offer). ``"plain_file"`` returns the gated plain-file backend,
    whose consent/flag/platform checks run in ``create_ref``.
    """
    if preferred is not None and preferred not in CREDENTIAL_BACKENDS:
        raise CredentialError(
            reason_code=CREDENTIAL_REF_INVALID,
            message="Unknown credential backend preference.",
            details={"valid_backends": list(CREDENTIAL_BACKENDS)},
        )
    resolved_env = env_backend or EnvRefCredentialBackend()
    if preferred == "env_ref":
        return resolved_env
    if preferred == "plain_file":
        return plain_file_backend or PlainFileCredentialBackend(
            capabilities=capabilities,
            allow_plain_secrets=allow_plain_secrets,
            consent=plain_file_consent,
        )
    resolved_keyring = keyring_backend or KeyringCredentialBackend()
    if resolved_keyring.is_available():
        return resolved_keyring
    return resolved_env


def store_provider_credential(
    request: CredentialRequest,
    *,
    preferred: str | None = None,
    capabilities: HostCredentialCapabilities,
    allow_plain_secrets: bool = False,
    plain_file_consent: bool = False,
    keyring_backend: CredentialBackend | None = None,
    env_backend: CredentialBackend | None = None,
    plain_file_backend: CredentialBackend | None = None,
) -> CredentialRef:
    """Select a backend and produce a safe credential reference for ``request``."""
    backend = select_credential_backend(
        preferred=preferred,
        capabilities=capabilities,
        allow_plain_secrets=allow_plain_secrets,
        plain_file_consent=plain_file_consent,
        keyring_backend=keyring_backend,
        env_backend=env_backend,
        plain_file_backend=plain_file_backend,
    )
    return backend.create_ref(request)


def _import_keyring_module() -> KeyringModule | None:
    """Lazily import the optional ``keyring`` library, or ``None`` if absent."""
    try:
        module = importlib.import_module("keyring")
    except ImportError:
        return None
    return cast("KeyringModule", module)


def _keyring_module_has_usable_backend(module: KeyringModule) -> bool:
    """Return whether the module resolves to a non no-op keyring backend."""
    get_keyring = getattr(module, "get_keyring", None)
    if not callable(get_keyring):
        return False
    try:
        backend = get_keyring()
    except Exception:
        # Resolving the keyring backend touches an unbounded third-party surface
        # (SecretService/DBus, KWallet, ...). Besides ``keyring.errors.KeyringError``
        # it can raise standard exceptions such as ``OSError`` (DBus unavailable)
        # or a bare ``RuntimeError``/``DBusException`` from a half-configured stack
        # on headless Linux. Treat any such failure as "no usable backend" so
        # selection degrades to env-ref instead of crashing on exactly the hosts
        # this fallback serves; ``BaseException`` (KeyboardInterrupt, SystemExit,
        # CancelledError) still propagates. Mirrors ``create_ref``'s write-path guard.
        return False
    if backend is None:
        return False
    return not _is_noop_keyring_backend(backend)


def _is_noop_keyring_backend(backend: object) -> bool:
    """Return whether a keyring backend is a fail/null no-op implementation."""
    backend_module = type(backend).__module__ or ""
    return backend_module.rsplit(".", 1)[-1] in _NOOP_KEYRING_BACKEND_LEAVES


def _pull_secret(request: CredentialRequest) -> str:
    """Pull the secret from the request; missing input fails non-interactively."""
    source = request.secret_source
    secret = source() if source is not None else None
    # A whitespace-only value is truthy but unusable for authentication; treat it
    # as missing input rather than writing it to the keychain/plain file. Mirrors
    # the env_ref name guard (``not env_var or not env_var.strip()``).
    if not secret or not secret.strip():
        raise _interactive_input_required(request, missing="secret")
    return secret


def _interactive_input_required(
    request: CredentialRequest,
    *,
    missing: str,
) -> CredentialError:
    """Build the non-interactive missing-input error with secret-free details."""
    return CredentialError(
        reason_code=INTERACTIVE_INPUT_REQUIRED,
        message="A required credential input is unavailable in a non-interactive run.",
        details={
            # ``provider`` reaches this builder unvalidated on the env_ref path:
            # ``EnvRefCredentialBackend`` raises here for a missing env var name
            # before (and instead of) routing ``provider`` through
            # ``_require_safe_identifier``, since env_ref never interpolates it.
            # Redact token-shaped substrings so a provider accidentally populated
            # with a raw secret never surfaces in these diagnostics or ``to_dict``.
            "provider": redact_audit_text(request.provider),
            "missing": missing,
            "non_interactive": request.non_interactive,
        },
    )


def _build_credential_ref(backend: CredentialBackendKind, ref: str) -> CredentialRef:
    """Build a safe reference, rejecting any ref a ``ProviderConfig`` can't store.

    Backends call this *before* their storage side effect: ``CredentialRef`` and
    ``ProviderConfig.credential_ref`` share a length cap, but provider/account/
    path identifiers are unbounded, so a long one would otherwise mint a ref that
    passes ``CredentialRef`` validation yet overflows the field that stores it.
    """
    if len(ref) > MAX_CREDENTIAL_REF_LENGTH:
        raise CredentialError(
            reason_code=CREDENTIAL_REF_INVALID,
            message="Credential reference exceeds the maximum storable length.",
            details={"backend": backend, "max_length": MAX_CREDENTIAL_REF_LENGTH},
        )
    return CredentialRef(backend=backend, ref=ref)


def _require_safe_identifier(value: str, *, field: str) -> str:
    """Return ``value`` if it is a safe ref/filename identifier, else reject it.

    A token-shaped value (e.g. ``sk-proj-...``/``github_pat_...``) still matches
    the filename-safe regex, so a provider/account accidentally populated with a
    raw secret would otherwise be interpolated into the keychain service name or
    plain-file path before ``CredentialRef`` rejects the resulting ref. Refuse it
    up front with the same secret-free reason code env var names use, so the token
    never reaches a storage path and the failure stays reason-coded.
    """
    if _TOKEN_SHAPE_RE.search(value) is not None:
        raise CredentialError(
            reason_code=CREDENTIAL_REF_INVALID,
            message=f"Credential {field} identifier resembles a raw secret value.",
            details={"field": field},
        )
    if not _SAFE_IDENTIFIER_RE.fullmatch(value):
        raise CredentialError(
            reason_code=CREDENTIAL_REF_INVALID,
            message=f"Credential {field} identifier is not a safe value.",
            details={"field": field},
        )
    return value


def _looks_like_secret(value: str) -> bool:
    """Return whether a value contains a token-shaped substring."""
    return _TOKEN_SHAPE_RE.search(value) is not None


def _default_secrets_dir() -> Path:
    """Return the default plain-file secrets directory (``~/.awf/secrets``)."""
    return Path(_DEFAULT_SECRETS_DIR).expanduser()


def _write_secret_file(target: Path, secret: str) -> None:
    """Atomically write ``secret`` to ``target`` with conservative permissions."""
    secrets_dir = target.parent
    tmp_path: Path | None = None
    try:
        # Create the leaf 0o700 at mkdir time, not loosened-then-tightened: a
        # plain ``mkdir`` honours the caller's umask (often 0o755), briefly
        # exposing the directory listing (provider names/structure) to other
        # users on a multi-user host until the chmod below lands. The chmod is
        # still needed for the ``exist_ok=True`` case, where ``mkdir`` leaves an
        # already-present looser directory untouched.
        secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _chmod_best_effort(secrets_dir, 0o700)
        tmp_path = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(secret)
        _chmod_best_effort(tmp_path, 0o600)
        tmp_path.replace(target)
        _chmod_best_effort(target, 0o600)
    except OSError as exc:
        if tmp_path is not None:
            with suppress(OSError):
                tmp_path.unlink()
        raise CredentialError(
            reason_code=CREDENTIAL_BACKEND_UNAVAILABLE,
            message="Unable to write the plain-file credential.",
            details={"error_type": type(exc).__name__},
        ) from exc


def _chmod_best_effort(path: Path, mode: int) -> None:
    """Apply POSIX file permissions when supported by the host platform."""
    if os.name != "posix":
        return
    with suppress(OSError):
        path.chmod(mode)


__all__ = [
    "CREDENTIAL_BACKENDS",
    "CREDENTIAL_BACKEND_UNAVAILABLE",
    "CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED",
    "CREDENTIAL_REF_INVALID",
    "INTERACTIVE_INPUT_REQUIRED",
    "MAX_CREDENTIAL_REF_LENGTH",
    "CredentialBackend",
    "CredentialBackendKind",
    "CredentialError",
    "CredentialRef",
    "CredentialRequest",
    "EnvRefCredentialBackend",
    "HostCredentialCapabilities",
    "KeyringCredentialBackend",
    "KeyringModule",
    "PlainFileCredentialBackend",
    "detect_host_credential_capabilities",
    "select_credential_backend",
    "store_provider_credential",
]
