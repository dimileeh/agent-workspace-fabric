"""Credential backend abstraction for AWF first-run host setup.

This module produces safe credential *references* (``keyring://``, ``env://``,
``plain-file://``) plus non-secret backend metadata. It never returns, logs, or
prints raw secret values, and it never prompts: missing required input in a
non-interactive run fails with ``INTERACTIVE_INPUT_REQUIRED``.

The keyring backend uses dependency injection and a lazy import, so tests
inject fakes and never touch a real OS keychain. ``keyring>=24`` is now a
required package dependency; the lazy import still degrades gracefully if it is
somehow missing, but that path is unreachable in a correctly installed
environment. Credential *resolution* (reading values back) and interactive
prompting belong to the setup/provider CLI flows (T04/T07), not here.
"""

from __future__ import annotations

import errno
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

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from awf.common.audit import REDACTION_MARKER, redact_audit_text
from awf.common.logging import get_logger
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
# Reuse the shared token recognizer so token-shaped inputs are redacted the same
# way as everywhere else in AWF. Case-insensitive on purpose: *diagnostic
# redaction* over-redacts case-variant shapes (e.g. ``SK-PROJ-...``) that a real
# secret would never use, which is safe.
_TOKEN_SHAPE_RE = compile_known_token_re(
    ignorecase=True,
    match_truncated_provider_tokens=True,
)
# Case-sensitive recognizer used to decide whether operator-supplied input *is* a
# raw secret value (and must be rejected). Real provider tokens always carry a
# lowercase prefix (``ghp_``/``github_pat_``/``sk-``/...), so folding case here —
# unlike for redaction above — is unsafe: it misclassifies a legitimate uppercase
# env-var name such as ``GITHUB_PAT_TOKEN`` or ``GHP_TOKEN`` as a token and blocks
# a valid env-ref setup path, even though env-ref only stores the name.
_TOKEN_VALUE_RE = compile_known_token_re(
    ignorecase=False,
    match_truncated_provider_tokens=True,
)
# ``keyring`` resolves to these no-op backends on hosts without a usable
# keychain (e.g. headless Linux); treat them as unavailable.
_NOOP_KEYRING_BACKEND_LEAVES = frozenset({"fail", "null"})

logger = get_logger(__name__)


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

    def get_password(self, service: str, username: str) -> str | None:
        """Return the stored secret for ``service``/``username``, or ``None``."""

    def delete_password(self, service: str, username: str) -> None:
        """Remove the stored secret for ``service``/``username`` if present."""


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
        # Verify the write actually persisted before returning a ``keyring://``
        # ref. ``is_available`` only rejects the ``fail``/``null`` no-op backends;
        # any other resolved backend passes that probe. On headless Linux
        # ``keyring.get_keyring()`` can resolve to a ``ChainerBackend`` with no
        # usable child: it is not a fail/null no-op, so it reports available and
        # accepts ``set_password`` without raising, yet stores nothing. Returning
        # a ref to that empty slot would silently fail at credential-resolution
        # time, so read the secret back and require it to round-trip. The
        # read-back touches the same unbounded third-party surface as the write,
        # so translate any failure into the same reason-coded error (callers
        # degrade to env-ref instead of trusting a ref that points at nothing);
        # ``BaseException`` still propagates.
        try:
            stored = module.get_password(service, account)
        except Exception as exc:
            # ``set_password`` already succeeded, so the secret may sit in the
            # keychain even though this read-back could not confirm durability.
            # ``store_provider_credential`` degrades a write-unusable keyring to
            # env_ref; discard the unverified write first so the recorded
            # ``env://NAME`` ref never diverges from a value left orphaned in the
            # OS keychain under a backend nothing will resolve through.
            _discard_keyring_write(module, service, account)
            raise CredentialError(
                reason_code=CREDENTIAL_BACKEND_UNAVAILABLE,
                message="The keyring backend rejected the credential read-back.",
                details={"backend": self.kind, "error_type": type(exc).__name__},
            ) from exc
        if stored != secret:
            # Same orphan hazard as the read-back-raises branch: a backend that
            # accepted the write yet did not round-trip it (a no-usable-child
            # ChainerBackend stores nothing, but a partial write may persist the
            # value) is about to be abandoned for env_ref, so best-effort delete
            # keeps the recorded backend and the keychain in sync.
            _discard_keyring_write(module, service, account)
            raise CredentialError(
                reason_code=CREDENTIAL_BACKEND_UNAVAILABLE,
                message="The keyring backend did not durably store the credential.",
                details={"backend": self.kind},
            )
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
        if not _ENV_VAR_NAME_RE.fullmatch(name):
            # The raw-secret guard catches an operator who pasted a token *value*
            # into the name field. It runs only once the input has already failed
            # the uppercase-identifier check: a valid env-var name such as
            # ``GITHUB_PAT_TOKEN`` or ``GHP_TOKEN`` can never be a real provider
            # token (those are lowercase-prefixed) and env-ref stores only the
            # name, never a value — scanning for token shape first (and
            # case-insensitively) would block that valid setup path. Prefer the
            # secret-shape message when the invalid input does look like a token.
            if _looks_like_secret(name):
                raise CredentialError(
                    reason_code=CREDENTIAL_REF_INVALID,
                    message="Environment variable name resembles a raw secret value.",
                    details={"field": "env_var"},
                )
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
        raw_secrets_dir = Path(secrets_dir) if secrets_dir is not None else _default_secrets_dir()
        # Expand a leading ``~``/``~user`` *before* absolutising. An explicit
        # ``secrets_dir="~/.awf/secrets"`` would otherwise have ``Path.absolute``
        # anchor the literal ``~`` under the cwd (``<repo>/~/.awf/secrets``),
        # stranding the approved secret in the workspace and pointing the
        # ``plain-file://`` ref away from the operator's host setup location.
        # ``expanduser`` only substitutes the home prefix; like ``absolute`` it
        # never traverses a symlink (only ``Path.resolve`` does), so the un-followed
        # path is preserved for the write-time guard below. The default path is
        # already expanded by ``_default_secrets_dir``, so this is a no-op there.
        #
        # Make the path absolute *without* following symlinks. ``Path.resolve``
        # would canonicalise a pre-planted ``~/.awf/secrets -> /attacker`` symlink
        # into its target here, stripping the very information
        # ``_mkdir_secure``'s ``is_symlink`` guard needs to refuse a redirected
        # secrets dir. ``Path.absolute`` only prepends the cwd (so a relative
        # ``secrets_dir`` still yields an absolute ``plain-file://`` ref) and never
        # traverses a link, preserving the un-followed path for the write-time
        # symlink check.
        self._secrets_dir = raw_secrets_dir.expanduser().absolute()

    def is_available(self) -> bool:
        """Return whether flag, consent, and a headless-Linux host all hold."""
        return (
            self._allow_plain_secrets and self._consent and self._capabilities.supports_plain_file
        )

    def create_ref(self, request: CredentialRequest) -> CredentialRef:
        """Write the secret to a ``0600`` file and return a ``plain-file://`` ref."""
        # Check the platform restriction before the flag/consent gate: it is the
        # more fundamental constraint, so a caller on macOS or desktop Linux who
        # has also not set the flag should learn the host is unsupported rather
        # than be sent down the consent path — adding the flag and consent on
        # that host would still be rejected here.
        if not self._capabilities.supports_plain_file:
            raise CredentialError(
                reason_code=CREDENTIAL_BACKEND_UNAVAILABLE,
                message="Plain-file credential storage is limited to headless Linux hosts.",
                details={
                    "os_name": self._capabilities.os_name,
                    "is_headless": self._capabilities.is_headless,
                },
            )
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
        provider = _require_safe_identifier(request.provider, field="provider")
        account = _require_safe_identifier(request.account, field="account")
        # Scope the secret file by account, mirroring the keyring backend's
        # ``service/account`` scoping: without it, two accounts for one provider
        # share a single ``<provider>`` file and the second write overwrites the
        # first. ``provider`` and ``account`` are validated to the safe-identifier
        # alphabet, which excludes ``.``, so a single ``.`` separator keeps the
        # pair unambiguous and the path one filename deep — preserving the
        # secrets-dir 0700 guarantee (a per-provider subdir would demote the
        # secrets dir to an intermediate ``mkdir`` parent that escapes the leaf
        # create-time mode).
        target = self._secrets_dir / f"{provider}.{account}"
        # Validate the ref before writing so an over-long path never leaves an
        # orphaned secret file the resulting ref could not be stored against.
        ref = _build_credential_ref("plain_file", f"plain-file://{target}")
        secret = _pull_secret(request)
        _write_secret_file(target, secret)
        return ref


def select_credential_backend(
    *,
    preferred: CredentialBackendKind | None,
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

    An explicit ``preferred="keyring"`` that falls back here (the cheap
    ``is_available()`` probe found no usable keychain) emits a secret-free
    ``host_setup.credential_backend_degraded`` warning so the degradation is
    observable, mirroring the write-time degradation path in
    ``store_provider_credential``. The ``None`` default stays silent: env_ref is
    its documented outcome on a keychain-less host, not a degradation.
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
    if preferred == "keyring":
        # An explicit keyring request that the cheap ``is_available()`` probe
        # rejects up front degrades to the env-ref offer. Mirror the write-time
        # degradation log in ``store_provider_credential`` so both routes are
        # equally observable: a caller who explicitly asked for keyring sees a
        # secret-free structured warning rather than only a changed
        # ``CredentialRef.backend``. ``preferred=None`` is the documented safe
        # default (headless-Linux-no-keychain → env-ref), not a degradation from an
        # explicit request, so it stays silent here to avoid warning on every
        # default selection. The write-time path never double-logs: when this
        # fires, keyring is not selected, so ``store_provider_credential`` mints
        # env_ref without re-entering its own degradation branch.
        logger.warning(
            "host_setup.credential_backend_degraded",
            requested_backend="keyring",
            preferred=preferred,
            effective_backend="env_ref",
            reason_code=CREDENTIAL_BACKEND_UNAVAILABLE,
        )
    return resolved_env


def store_provider_credential(
    request: CredentialRequest,
    *,
    preferred: CredentialBackendKind | None = None,
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
    try:
        return backend.create_ref(request)
    except CredentialError as exc:
        # ``select_credential_backend`` returns the keyring backend whenever its
        # ``is_available`` probe passes, but that probe is a cheap, side-effect-free
        # pre-filter that only rejects the ``fail``/``null`` no-op backends. A
        # headless-Linux ``ChainerBackend`` with no usable child passes it, so
        # keyring is selected, yet only the write/read-back round-trip *inside*
        # ``create_ref`` can prove it cannot durably store a secret
        # (``CREDENTIAL_BACKEND_UNAVAILABLE``). The pre-``create_ref`` fallback to
        # env_ref fires only for the backends the probe can reject up front, so
        # degrade here too: a keyring proven unusable at write time must reach the
        # same env-ref offer as one detected unusable beforehand (the documented
        # headless-Linux default; plan R5), not fail setup. Only the default
        # keyring selection degrades — an explicit ``plain_file``/``env_ref``
        # preference yields a non-keyring ``backend.kind`` and never enters this
        # branch — and only the backend-unavailable signal degrades: a genuine
        # input fault (``INTERACTIVE_INPUT_REQUIRED`` missing secret,
        # ``CREDENTIAL_REF_INVALID`` token-shaped/over-long identifier) must
        # propagate, since env_ref consumes only ``env_var`` and would otherwise
        # silently mask the rejected provider/account or the missing secret.
        if backend.kind != "keyring" or exc.reason_code != CREDENTIAL_BACKEND_UNAVAILABLE:
            raise
        # The selected keyring backend passed its cheap ``is_available`` probe but
        # proved unusable only at write time, so the credential degrades to the
        # env-ref offer. That degradation is otherwise observable to callers only as
        # a changed ``CredentialRef.backend``; emit a structured, secret-free warning
        # so a caller who explicitly requested keyring (``preferred="keyring"``) can
        # tell the stored ref now points at an env var rather than the OS keychain.
        # ``provider`` is already validated by ``create_ref`` before any
        # backend-unavailable signal, but redact it defensively in case it carried a
        # token-shaped value.
        logger.warning(
            "host_setup.credential_backend_degraded",
            requested_backend="keyring",
            preferred=preferred,
            effective_backend="env_ref",
            reason_code=exc.reason_code,
            provider=_redact_token_shaped(request.provider),
        )
        # Mirror the pre-``create_ref`` path exactly: with an ``env_var`` this mints
        # the ``env://NAME`` offer; without one it raises ``INTERACTIVE_INPUT_REQUIRED``
        # (the actionable "provide an env var" signal), never the raw keyring error.
        fallback = env_backend or EnvRefCredentialBackend()
        return fallback.create_ref(request)


def _discard_keyring_write(module: KeyringModule, service: str, account: str) -> None:
    """Best-effort delete a just-written secret whose durability is unproven.

    Called only from ``KeyringCredentialBackend.create_ref``'s read-back-failure
    paths, where ``set_password`` already succeeded but the round-trip could not
    confirm the secret is durably stored. The caller then degrades to env_ref;
    removing the value first ensures the recorded ``env://NAME`` ref never
    diverges from a secret still living in the keychain. ``delete_password``
    touches the same unbounded third-party surface as the write (and may be
    missing on a partial module, or raise ``PasswordDeleteError`` when the slot
    is already empty), so every failure is swallowed: cleanup is best-effort and
    must never mask the underlying backend-unavailable signal. ``BaseException``
    (KeyboardInterrupt, SystemExit, CancelledError) still propagates.
    """
    with suppress(Exception):
        module.delete_password(service, account)


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
    """Return whether a keyring backend is a fail/null no-op implementation.

    This is a cheap, side-effect-free pre-filter for the ``fail``/``null``
    backends ``keyring`` resolves to when no keychain exists. It deliberately
    does not enumerate every non-durable backend (e.g. a ``ChainerBackend`` with
    no usable child on headless Linux passes here): the authoritative durability
    guarantee is the write/read-back round-trip in
    ``KeyringCredentialBackend.create_ref``, which refuses to mint a ref when the
    secret does not survive storage.

    The leaf match is scoped to ``keyring``-rooted modules so a fully functional
    third-party backend that merely shares the leaf name (e.g. ``myvault.null``)
    is not silently treated as unavailable and degraded to env-ref.
    """
    backend_module = type(backend).__module__ or ""
    leaf = backend_module.rsplit(".", 1)[-1]
    return leaf in _NOOP_KEYRING_BACKEND_LEAVES and backend_module.startswith("keyring.")


def _pull_secret(request: CredentialRequest) -> str:
    """Pull the secret from the request; missing input fails non-interactively."""
    source = request.secret_source
    secret = source() if source is not None else None
    # A whitespace-only value is truthy but unusable for authentication; treat it
    # as missing input rather than writing it to the keychain/plain file. Strip
    # surrounding whitespace from the stored value too, so a padded secret (a
    # common copy-paste artifact) is normalised instead of written verbatim and
    # silently failing auth later. Mirrors the env_ref name path, which strips
    # its identifier (``name = env_var.strip()``) before use.
    stripped = secret.strip() if secret else None
    if not stripped:
        raise _interactive_input_required(request, missing="secret")
    return stripped


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
            "provider": _redact_token_shaped(request.provider),
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

    The final ``CredentialRef`` construction can also fail ``_validate_ref`` for
    reasons the length check does not cover — e.g. an unsanitised ``secrets_dir``
    path component that is token-shaped trips the raw-secret guard. Every backend
    promises callers it raises only ``CredentialError``, so wrap that
    ``ValidationError`` into a reason-coded error instead of letting Pydantic's
    raw exception escape ``store_provider_credential``.
    """
    if len(ref) > MAX_CREDENTIAL_REF_LENGTH:
        raise CredentialError(
            reason_code=CREDENTIAL_REF_INVALID,
            message="Credential reference exceeds the maximum storable length.",
            details={"backend": backend, "max_length": MAX_CREDENTIAL_REF_LENGTH},
        )
    try:
        return CredentialRef(backend=backend, ref=ref)
    except ValidationError as exc:
        # ``CredentialRef`` sets ``hide_input_in_errors``, so ``exc`` carries no
        # raw ref; surface only the secret-free backend kind in the reason code.
        raise CredentialError(
            reason_code=CREDENTIAL_REF_INVALID,
            message="Credential reference failed internal validation.",
            details={"backend": backend},
        ) from exc


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
    """Return whether a value contains a raw secret (token-shaped) substring.

    Uses the case-sensitive recognizer: a value is treated as a secret only when
    it carries a real provider token's lowercase prefix, so a legitimate
    uppercase env-var name (e.g. ``GITHUB_PAT_TOKEN``/``GHP_TOKEN``) is not
    mistaken for one. The case-insensitive ``_TOKEN_SHAPE_RE`` stays reserved for
    diagnostic redaction, where over-matching is safe.
    """
    return _TOKEN_VALUE_RE.search(value) is not None


def _redact_token_shaped(value: str) -> str:
    """Redact token-shaped substrings from a value bound for error diagnostics.

    ``redact_audit_text`` uses a *case-sensitive* known-token recognizer, but
    this module's own token-shape checks (``_TOKEN_SHAPE_RE``) are
    case-insensitive. Fold case here first via ``_TOKEN_SHAPE_RE`` so a
    case-variant token-shaped value (e.g. ``SK-PROJ-...``) is redacted rather
    than slipping past the audit helper and surfacing verbatim; then defer to
    ``redact_audit_text`` for its other redactions (URL/bearer/assignment) and
    the length cap.
    """
    return redact_audit_text(_TOKEN_SHAPE_RE.sub(REDACTION_MARKER, value))


def _default_secrets_dir() -> Path:
    """Return the default plain-file secrets directory (``~/.awf/secrets``)."""
    return Path(_DEFAULT_SECRETS_DIR).expanduser()


def _write_secret_file(target: Path, secret: str) -> None:
    """Atomically write ``secret`` to ``target`` with conservative permissions."""
    secrets_dir = target.parent
    tmp_path: Path | None = None
    try:
        _mkdir_secure(secrets_dir)
        tmp_path = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
        # ``O_EXCL`` makes the kernel create a fresh inode and refuse the open if
        # anything already exists at ``tmp_path`` — including a symlink an attacker
        # planted to redirect this secret write to a path they control. The 0o700
        # secrets dir and 8-byte random suffix already make that extremely
        # unlikely, but ``O_EXCL`` is cheap defence-in-depth for a secrets write.
        # A pre-existing temp path then raises ``FileExistsError`` (an ``OSError``),
        # cleaned up and reason-coded by the outer handler below.
        # ``O_TRUNC`` is intentionally omitted: ``O_EXCL | O_CREAT`` already
        # guarantees a freshly created (and therefore empty) inode, so truncation
        # would be a no-op. Keeping the flag set minimal lets every flag explain
        # why it is present.
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        # ``os.fdopen`` takes ownership of ``fd`` only once it returns; if it
        # raises (e.g. an invalid mode or an implementation-level error) the raw
        # descriptor would leak. CPython closes it internally on an ``os.fdopen``
        # failure, but that is undocumented and not guaranteed across Python
        # implementations, so close it explicitly to keep the cleanup contract
        # portable. The outer handler still unlinks the temp file.
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8")
        except Exception:
            os.close(fd)
            raise
        with handle:
            handle.write(secret)
            # Force the secret bytes to disk before the atomic rename so a power
            # failure cannot leave a renamed-but-empty target behind.
            handle.flush()
            os.fsync(handle.fileno())
        _chmod_best_effort(tmp_path, 0o600)
        tmp_path.replace(target)
        _chmod_best_effort(target, 0o600)
        # The fsync above made the secret *bytes* durable, but the rename only
        # mutates the parent directory entry. On a filesystem mounted
        # ``data=writeback`` a crash after the rename but before the directory
        # journal flushes can roll the rename back, leaving the freshly minted
        # ``plain-file://`` ref pointing at a path that reverted or vanished.
        # Fsync the directory so the rename itself is durable. Best-effort: the
        # write already succeeded, so a dir-sync failure must not be reported as
        # a write failure (which would discard a credential that is on disk).
        _fsync_dir_best_effort(secrets_dir)
    except Exception as exc:  # noqa: BLE001 - normalize any write failure to CredentialError
        # Cleanup + reason-coding must cover non-``OSError`` failures too: a
        # secret that cannot be UTF-8 encoded raises ``UnicodeEncodeError`` from
        # ``handle.write``, which would otherwise orphan the 0600 temp file in
        # the 0700 secrets dir and escape this backend's "only raises
        # ``CredentialError``" contract with a raw ``ValueError``.
        if tmp_path is not None:
            with suppress(OSError):
                tmp_path.unlink()
        raise CredentialError(
            reason_code=CREDENTIAL_BACKEND_UNAVAILABLE,
            message="Unable to write the plain-file credential.",
            details={"error_type": type(exc).__name__},
        ) from exc


def _mkdir_secure(directory: Path) -> None:
    """Create ``directory`` and every ancestor we create with mode 0o700.

    ``Path.mkdir(parents=True, mode=0o700)`` applies the restrictive mode only to
    the leaf: intermediate parents (e.g. ``~/.awf`` beneath ``~/.awf/secrets``)
    are created with umask-default permissions, which can leave the parent
    world-traversable on a multi-user host. Walk the chain of not-yet-existing
    ancestors and create each with a restrictive create-time mode so 0o700 lands
    on every level we create, while leaving pre-existing ancestors (such as the
    home directory) untouched.

    The create-time mode (not a loosen-then-tighten chmod) closes the brief
    TOCTOU window where a permissive umask would expose the directory listing
    (provider names/structure) until a later chmod lands. The trailing
    best-effort chmod still tightens the leaf for the ``exist_ok`` case, where
    ``mkdir`` leaves an already-present looser directory untouched.

    Symlinks are refused, never followed: a pre-planted ``~/.awf/secrets ->
    /attacker`` (or a symlinked ancestor of the secrets dir, whether the dir is
    about to be created under it or already exists beneath it) would otherwise
    redirect both the hardening chmod and every subsequent secret write into an
    attacker-controlled target. The ``exists``/``is_dir`` probes below all follow
    symlinks, so the file-level ``O_EXCL`` in ``_write_secret_file`` — which only
    protects the temp inode, not the directory it lands in — would not catch it.
    ``is_symlink`` (``lstat``) is the non-following check that does, applied to
    every ancestor up to the filesystem root (not merely the nearest pre-existing
    one, which would miss a symlinked grandparent above an already-created
    descendant).
    """
    # Refuse a symlinked leaf up front so it is rejected whether its target
    # exists (``exists()``/``is_dir()`` would follow it) or dangles (``mkdir``
    # on a dangling-symlink leaf would otherwise raise a confusing
    # ``FileExistsError``). ``lstat``-based, so it never traverses the link.
    if directory.is_symlink():
        raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), str(directory))
    # Walk the ancestor chain all the way to the filesystem root, refusing any
    # symlink along the way and recording the not-yet-existing levels to create.
    # The walk must *not* stop at the nearest pre-existing ancestor: ``is_symlink``
    # (``lstat``) only inspects the *final* component of the path it is handed, so a
    # symlinked ancestor is caught only once the walk reaches it as that final
    # component. A symlinked grandparent above an existing descendant — e.g.
    # ``/tmp/awf -> /tmp/attacker`` with ``/tmp/attacker/profile/secrets`` planted so
    # the whole configured ``/tmp/awf/profile/secrets`` tree already "exists" through
    # the link — is otherwise skipped: probing ``/tmp/awf/profile`` finds
    # ``is_symlink()`` false (the link is an intermediate component there) and
    # ``exists()`` true, so a break-on-existing walk returns before ever
    # ``lstat``-ing ``/tmp/awf`` itself, and the hardening chmod plus every secret
    # write then follow the link into the attacker tree. Continuing past existing
    # descendants makes each ancestor in turn the final component of an
    # ``is_symlink`` check, so a redirect anywhere in the chain is refused.
    #
    # This deliberately refuses *any* symlinked ancestor, including legitimately
    # symlinked system levels (e.g. ``/var -> /private/var`` above a temp secrets
    # dir): an attacker-planted redirect and a benign OS symlink are structurally
    # indistinguishable by path inspection alone (same shape: a symlinked ancestor
    # with real descendants), so the secrets write fails closed and the operator
    # points ``secrets_dir`` at an un-symlinked path. The default ``~/.awf/secrets``
    # under a real home directory is unaffected.
    missing: list[Path] = []
    if not directory.exists():
        missing.append(directory)
    probe = directory.parent
    while True:
        # ``is_symlink`` (``lstat``) on each ancestor is the non-following check:
        # the ``exists``/``is_dir`` probes follow links, so this is what actually
        # catches an attacker-redirected ancestor of the secrets dir.
        if probe.is_symlink():
            raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), str(probe))
        if not probe.exists():
            # A not-yet-existing level the create loop will mkdir + harden below.
            # Existence is monotonic up the chain, so every level recorded here is
            # contiguous with the leaf; pre-existing ancestors are left untouched
            # (merely symlink-checked) and never enter ``missing``.
            missing.append(probe)
        parent = probe.parent
        if parent == probe:  # reached the filesystem root.
            break
        probe = parent
    for path in reversed(missing):
        # ``exist_ok`` is intentionally omitted so the create is *atomic*: every
        # entry in ``missing`` was confirmed absent (and the leaf non-symlink) by
        # the walk above, so a bare ``mkdir`` either creates a fresh real directory
        # or fails. ``exist_ok=True`` would instead mask a TOCTOU race — if an
        # attacker plants a ``component -> /attacker`` symlink in the window between
        # the ``lstat`` check above and this create, ``mkdir(exist_ok=True)``
        # swallows the ``FileExistsError`` (its follow-up ``is_dir()`` follows the
        # link to a real directory) and silently accepts the link, so the hardening
        # chmod and every subsequent secret write follow it into the attacker
        # target — defeating the "symlinks are refused, never followed" guarantee.
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            # Something raced into this component between the symlink walk and the
            # create. Re-``lstat`` (``is_symlink``, non-following) and refuse a
            # symlink (the attack above) or any non-directory, so neither the
            # hardening chmod nor a later secret write follows it. A real directory
            # built concurrently by another trusted run is tolerated (benign
            # ``mkdir -p`` idempotency). ``is_symlink`` short-circuits before the
            # link-following ``is_dir`` so the check itself never traverses a link.
            if path.is_symlink() or not path.is_dir():
                raise NotADirectoryError(
                    errno.ENOTDIR, os.strerror(errno.ENOTDIR), str(path)
                ) from None
        _chmod_best_effort(path, 0o700)
    # A ``directory`` that already existed as a regular file (or a symlink to
    # one) never enters the creation loop above — ``probe.exists()`` is true on
    # the first iteration, so ``missing`` stays empty. Without this guard the
    # final hardening chmod would re-permission that unrelated file to 0o700
    # before the later secret-file ``os.open`` fails with ``NotADirectoryError``,
    # silently mutating e.g. a ``~/.awf/secrets`` accidentally created as a file.
    # Refuse a non-directory here so a doomed plain-file setup leaves it alone.
    if not directory.is_dir():
        raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), str(directory))
    _chmod_best_effort(directory, 0o700)
    # The hardening chmod above is *best-effort*: when ``secrets_dir`` points at a
    # pre-existing directory the operator does not own (e.g. ``/tmp`` or a
    # root-owned 0777 shared dir), the ``chmod`` raises and ``_chmod_best_effort``
    # suppresses it, silently leaving the dir group/world-accessible. A freshly
    # created level is safe (its 0o700 create-time mode already holds, only ever
    # tightened by the umask), but this explicit-path fallback would otherwise
    # write the secret and mint a ``plain-file://`` ref inside a world-traversable
    # dir — demoting the plain-file backend's own 0700 directory guarantee. Verify
    # the result and fail closed so the secret never lands in a shared directory.
    _reject_group_or_world_accessible_dir(directory)


def _reject_group_or_world_accessible_dir(directory: Path) -> None:
    """Refuse ``directory`` if any group/other permission bit survives, on POSIX.

    The owner-private (0o700) guarantee means group and other must hold no read,
    write, or execute bit. ``directory`` has been confirmed a real (non-symlink)
    directory by the guards in ``_mkdir_secure``, so ``stat`` reflects the
    directory itself rather than a redirected link target. POSIX-only: the
    rwx-for-group/other model does not apply off POSIX, where ``_chmod_best_effort``
    is itself a no-op, so a non-POSIX ``st_mode`` carrying those bits is ignored
    rather than treated as a hardening failure.
    """
    if os.name != "posix":
        return
    if directory.stat().st_mode & 0o077:
        raise PermissionError(
            errno.EPERM,
            "Secrets directory is group- or world-accessible and could not be hardened to 0700.",
            str(directory),
        )


def _chmod_best_effort(path: Path, mode: int) -> None:
    """Apply POSIX file permissions when supported by the host platform."""
    if os.name != "posix":
        return
    with suppress(OSError):
        path.chmod(mode)


def _fsync_dir_best_effort(directory: Path) -> None:
    """Fsync ``directory`` so a contained rename's directory entry is durable.

    Directory fsync is POSIX-only (on Windows ``os.open`` on a directory fails),
    so it is a no-op off POSIX. It is also best-effort even on POSIX: the secret
    file has already been written and renamed into place by the time this runs,
    so a sync failure here is a durability-hardening miss, not a write failure —
    surfacing it would wrongly discard a credential that is already on disk.
    """
    if os.name != "posix":
        return
    with suppress(OSError):
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


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
