"""Resolve profile-declared local secret leases into compose-safe inputs."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from awf.common.git_auth import (
    apply_bitbucket_agent_git_auth,
    bitbucket_askpass_script,
)
from awf.common.immutability import frozen_mapping
from awf.node.compose_manager import AuthMount
from awf.profiles.models import ProfileSecret, WorkspaceProfile

SECRET_LEASE_PROVIDER_UNSUPPORTED = "SECRET_LEASE_PROVIDER_UNSUPPORTED"
SECRET_LEASE_SOURCE_MISSING = "SECRET_LEASE_SOURCE_MISSING"
SECRET_LEASE_SOURCE_TOO_BROAD = "SECRET_LEASE_SOURCE_TOO_BROAD"
SECRET_LEASE_SOURCE_INVALID = "SECRET_LEASE_SOURCE_INVALID"
SECRET_LEASE_TARGET_MISMATCH = "SECRET_LEASE_TARGET_MISMATCH"
SECRET_LEASE_TARGET_KIND_MISMATCH = "SECRET_LEASE_TARGET_KIND_MISMATCH"
SECRET_LEASE_WRITABLE_UNSUPPORTED = "SECRET_LEASE_WRITABLE_UNSUPPORTED"

_ENV_PROVIDERS = frozenset(("env",))
_GITHUB_PROVIDERS = frozenset(("github",))
_BITBUCKET_PROVIDERS = frozenset(("bitbucket",))
_LOCAL_FILE_PROVIDERS = frozenset(("local-file", "host-file"))
_LOCAL_AUTH_PROVIDERS = frozenset(("local-auth", "auth"))
_SUPPORTED_PROVIDERS = (
    _ENV_PROVIDERS
    | _GITHUB_PROVIDERS
    | _BITBUCKET_PROVIDERS
    | _LOCAL_FILE_PROVIDERS
    | _LOCAL_AUTH_PROVIDERS
)
_GITHUB_TOKEN_SOURCE_NAMES = ("AWF_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
_GITHUB_TOKEN_TARGET_NAMES = frozenset(("GH_TOKEN", "GITHUB_TOKEN"))
# BitBucket Cloud git/REST credentials (issue #461). Each declared target maps
# to the source env var(s) it may be filled from; the resolver emits a Compose
# ``${VAR}`` placeholder, never the secret value itself.
_BITBUCKET_TARGET_SOURCE_NAMES: dict[str, tuple[str, ...]] = {
    "BITBUCKET_API_TOKEN": ("BITBUCKET_API_TOKEN",),
    "BITBUCKET_EMAIL": ("BITBUCKET_EMAIL",),
}
# Agent-side BitBucket git-over-HTTPS auth (issues #465/#466). When the token
# target below is injected into the agent env, the resolver also materializes a
# static ``GIT_ASKPASS`` script (no token — only the env-var *name*) and mounts
# it read-only at the askpass target, then wires ``GIT_ASKPASS`` +
# ``insteadOf`` into the agent env. The token VALUE never lands in any rendered
# string; only ``${BITBUCKET_API_TOKEN}`` (the lease placeholder) and the
# runtime ``$BITBUCKET_API_TOKEN`` inside the mounted script reference it.
_BITBUCKET_GIT_TOKEN_TARGET = "BITBUCKET_API_TOKEN"
_BITBUCKET_ASKPASS_TARGET = "/run/awf/secrets/bb-askpass.sh"
_BITBUCKET_ASKPASS_FILENAME = "bb-askpass.sh"
_SECRET_LEASE_MATERIALIZATION_SUBDIR = "secret-leases"
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HOME_ROOT_RE = re.compile(r"^/(?:home|Users)/[^/]+/?$")

_KNOWN_LOCAL_AUTH_REFS: dict[str, tuple[str, str]] = {
    "gh": (".config/gh", "/home/agent/.config/gh"),
    ".config/gh": (".config/gh", "/home/agent/.config/gh"),
    "gcloud": (".config/gcloud", "/home/agent/.config/gcloud"),
    ".config/gcloud": (".config/gcloud", "/home/agent/.config/gcloud"),
    "gitconfig": (".gitconfig", "/home/agent/.gitconfig"),
    ".gitconfig": (".gitconfig", "/home/agent/.gitconfig"),
    "ssh": (".ssh", "/home/agent/.ssh"),
    ".ssh": (".ssh", "/home/agent/.ssh"),
}
_SAFE_SECRET_MOUNT_PREFIXES = ("/run/awf/secrets/", "/run/secrets/")


class SecretLeaseResolutionError(ValueError):
    """Structured local secret lease resolution failure.

    The exception deliberately omits provider refs and source values. Those can
    be secret-adjacent paths or tokens; callers should rely on ``reason_code``,
    ``secret_name``, ``provider``, and ``target`` for diagnostics.
    """

    def __init__(
        self,
        *,
        reason_code: str,
        secret_name: str,
        provider: str | None,
        target: str,
        kind: str | None = None,
    ) -> None:
        self.reason_code = reason_code
        self.secret_name = secret_name
        self.provider = provider
        self.target = target
        self.kind = kind
        provider_label = provider or "unspecified"
        kind_label = f", kind={kind}" if kind else ""
        super().__init__(
            f"{reason_code}: secret={secret_name}, provider={provider_label}, "
            f"target={target}{kind_label}"
        )


@dataclass(frozen=True)
class LocalSecretLeaseResolution:
    """Compose inputs derived from profile-declared local secret leases."""

    environment: tuple[tuple[str, str], ...] = ()
    mounts: tuple[AuthMount, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    satisfied_legacy_targets: frozenset[str] = frozenset()
    satisfied_legacy_providers: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", frozen_mapping(self.metadata))


@dataclass(frozen=True)
class LocalSecretLeaseMountResolver:
    """Resolve local-mode profile secret declarations.

    This resolver never returns secret values. Env leases become Compose
    placeholders like ``${OPENAI_API_KEY}``; mount leases become exact read-only
    bind mounts; metadata contains only counts, providers, targets, and omitted
    optional declarations.
    """

    host_home: Path
    work_dir: Path
    host_env: Mapping[str, str] | None = None

    def resolve(
        self,
        profile: WorkspaceProfile,
        *,
        workspace_id: str,
    ) -> LocalSecretLeaseResolution:
        source_env = os.environ if self.host_env is None else self.host_env
        # When a profile already declares GIT_ASKPASS in its runtime environment,
        # StackLauncher merges the lease env on top WITHOUT clobbering it
        # (``merge_agent_environment``), so the profile's GIT_ASKPASS wins in the
        # container. Wiring the bitbucket askpass block in that case would emit
        # ``insteadOf`` rewrites that force an askpass password while Git invokes
        # the profile's unrelated script — breaking private Bitbucket fetch/push.
        # Detect it here so the auth block stays gated on the effective agent env.
        profile_presets_git_askpass = "GIT_ASKPASS" in profile.runtime.environment
        env: dict[str, str] = {}
        mounts: list[AuthMount] = []
        providers: list[str] = []
        targets: list[str] = []
        # Counts only profile-declared lease env pairs. AWF-internal git-auth env
        # vars (GIT_ASKPASS/GIT_CONFIG_* injected by the bitbucket askpass wiring)
        # land in ``env`` too, so ``len(env)`` over-counts declared leases; this
        # keeps ``env_count`` a faithful declared-lease proxy.
        lease_env_count = 0
        omitted_optional: list[dict[str, str]] = []
        satisfied_legacy_targets: set[str] = set()
        satisfied_legacy_providers: set[str] = set()
        skipped_unresolved_count = 0
        # Whether a bitbucket git-token lease was injected. The agent-side
        # askpass wiring decision is deferred until after the loop (below) so it
        # observes the *final* resolved env and is independent of secret ordering.
        bitbucket_git_token_injected = False

        for secret in profile.secrets:
            provider = _normalized_provider(secret)
            if provider is None:
                skipped_unresolved_count += 1
                continue
            if provider not in _SUPPORTED_PROVIDERS:
                self._raise(
                    SECRET_LEASE_PROVIDER_UNSUPPORTED,
                    secret,
                    provider=provider,
                )

            if provider in _ENV_PROVIDERS:
                pair = self._resolve_env_secret(
                    secret,
                    source_env=source_env,
                    provider=provider,
                    omitted_optional=omitted_optional,
                )
                if pair is None:
                    continue
                _append_env_pair(env, pair)
                lease_env_count += 1
                _append_unique(providers, provider)
                _append_unique(targets, pair[0])
                continue

            if provider in _GITHUB_PROVIDERS:
                pairs = self._resolve_github_secret(
                    secret,
                    source_env=source_env,
                    provider=provider,
                    omitted_optional=omitted_optional,
                )
                if not pairs:
                    continue
                for pair in pairs:
                    _append_env_pair(env, pair)
                    lease_env_count += 1
                    _append_unique(targets, pair[0])
                _append_unique(providers, provider)
                satisfied_legacy_providers.add("github")
                continue

            if provider in _BITBUCKET_PROVIDERS:
                pair = self._resolve_bitbucket_secret(
                    secret,
                    source_env=source_env,
                    provider=provider,
                    omitted_optional=omitted_optional,
                )
                if pair is None:
                    continue
                _append_env_pair(env, pair)
                lease_env_count += 1
                _append_unique(providers, provider)
                _append_unique(targets, pair[0])
                # Record the git-token injection; the askpass wiring runs once
                # after the loop (the email lease, REST basic auth, is independent
                # and does not trigger it).
                if pair[0] == _BITBUCKET_GIT_TOKEN_TARGET:
                    bitbucket_git_token_injected = True
                continue

            if provider in _LOCAL_FILE_PROVIDERS:
                mount = self._resolve_local_file_secret(
                    secret,
                    provider=provider,
                    omitted_optional=omitted_optional,
                )
                if mount is None:
                    continue
                mounts.append(mount)
                _append_unique(providers, provider)
                _append_unique(targets, mount.target)
                continue

            mount = self._resolve_local_auth_secret(
                secret,
                provider=provider,
                omitted_optional=omitted_optional,
            )
            if mount is None:
                continue
            mounts.append(mount)
            _append_unique(providers, provider)
            _append_unique(targets, mount.target)
            if mount.target in _known_local_auth_targets():
                satisfied_legacy_targets.add(mount.target)
            if mount.target == "/home/agent/.config/gh":
                satisfied_legacy_providers.add("github")

        if (
            bitbucket_git_token_injected
            and "GIT_ASKPASS" not in env
            and not profile_presets_git_askpass
        ):
            # Wire agent-side git-over-HTTPS auth (askpass file + insteadOf) once,
            # AFTER every lease is resolved, so the gate observes the final agent
            # env. Deferring past the loop makes the decision independent of secret
            # ordering: a profile that declares its own GIT_ASKPASS — whether in
            # runtime.environment (``profile_presets_git_askpass``) OR via an env
            # lease (``target: GIT_ASKPASS``, now reflected in ``env`` regardless
            # of where it sits relative to the token lease) — owns askpass, and
            # AWF must not contradict it with insteadOf rewrites that only the AWF
            # askpass can satisfy (issue #466). Gating on GIT_ASKPASS being absent
            # from the resolved lease env also means a duplicate token lease cannot
            # mount the script twice.
            self._materialize_bitbucket_agent_askpass(
                env,
                mounts,
                workspace_id=workspace_id,
                token_env_var=_BITBUCKET_GIT_TOKEN_TARGET,
            )

        metadata: dict[str, Any] = {
            "schema": "secret_lease_mount_metadata.v1",
            "mount_plan": "profile_declared_secret_leases",
            "env_count": lease_env_count,
            "total_env_count": len(env),
            "mount_count": len(mounts),
            "providers": providers,
            "targets": targets,
        }
        if omitted_optional:
            metadata["omitted_optional_count"] = len(omitted_optional)
            metadata["omitted_optional"] = omitted_optional
        if skipped_unresolved_count:
            metadata["skipped_unresolved_count"] = skipped_unresolved_count
        return LocalSecretLeaseResolution(
            environment=tuple(env.items()),
            mounts=tuple(mounts),
            metadata=metadata,
            satisfied_legacy_targets=frozenset(satisfied_legacy_targets),
            satisfied_legacy_providers=frozenset(satisfied_legacy_providers),
        )

    def _resolve_env_secret(
        self,
        secret: ProfileSecret,
        *,
        source_env: Mapping[str, str],
        provider: str,
        omitted_optional: list[dict[str, str]],
    ) -> tuple[str, str] | None:
        if secret.kind != "env":
            self._raise(SECRET_LEASE_TARGET_KIND_MISMATCH, secret, provider=provider)
        env_name = _env_ref_name(secret.ref)
        if env_name is None:
            self._raise(SECRET_LEASE_SOURCE_INVALID, secret, provider=provider)
        if not source_env.get(env_name):
            self._missing(secret, provider=provider, omitted_optional=omitted_optional)
            return None
        return (secret.target, f"${{{env_name}}}")

    def _resolve_github_secret(
        self,
        secret: ProfileSecret,
        *,
        source_env: Mapping[str, str],
        provider: str,
        omitted_optional: list[dict[str, str]],
    ) -> tuple[tuple[str, str], ...]:
        if secret.kind != "env":
            self._raise(SECRET_LEASE_TARGET_KIND_MISMATCH, secret, provider=provider)
        if secret.target not in _GITHUB_TOKEN_TARGET_NAMES:
            self._raise(SECRET_LEASE_TARGET_MISMATCH, secret, provider=provider)
        source_name = next(
            (name for name in _GITHUB_TOKEN_SOURCE_NAMES if source_env.get(name)), None
        )
        if source_name is None:
            self._missing(secret, provider=provider, omitted_optional=omitted_optional)
            return ()
        placeholder = f"${{{source_name}}}"
        return (("GH_TOKEN", placeholder), ("GITHUB_TOKEN", placeholder))

    def _resolve_bitbucket_secret(
        self,
        secret: ProfileSecret,
        *,
        source_env: Mapping[str, str],
        provider: str,
        omitted_optional: list[dict[str, str]],
    ) -> tuple[str, str] | None:
        if secret.kind != "env":
            self._raise(SECRET_LEASE_TARGET_KIND_MISMATCH, secret, provider=provider)
        source_names = _BITBUCKET_TARGET_SOURCE_NAMES.get(secret.target)
        if source_names is None:
            self._raise(SECRET_LEASE_TARGET_MISMATCH, secret, provider=provider)
        source_name = next((name for name in source_names if source_env.get(name)), None)
        if source_name is None:
            self._missing(secret, provider=provider, omitted_optional=omitted_optional)
            return None
        return (secret.target, f"${{{source_name}}}")

    def _materialize_bitbucket_agent_askpass(
        self,
        env: dict[str, str],
        mounts: list[AuthMount],
        *,
        workspace_id: str,
        token_env_var: str,
    ) -> None:
        """Materialize + mount the agent-side BitBucket ``GIT_ASKPASS`` script.

        Writes a **static** askpass script (containing no token — only the
        ``token_env_var`` *name*) to a per-workspace subdir of ``work_dir``,
        marks it world read+execute (``0o555``) so the agent (uid 1000) can
        execute the read-only bind mount, mounts it at the askpass target, and
        wires ``GIT_ASKPASS`` + ``insteadOf`` into ``env``. The token VALUE never
        appears in the script, the mount, or ``env`` — only the runtime
        ``$BITBUCKET_API_TOKEN`` reference, resolved inside the container at git
        call time. The bind-mount source uses ``work_dir`` directly, matching the
        host-equal convention used for the other auth-mount sources.
        """
        # Defense in depth: ``workspace_id`` is internal AWF data (``ws_`` + hex),
        # but it is used here to build a filesystem path. Reject empty ids, path
        # separators, or traversal segments so a future source change cannot escape
        # ``work_dir`` or collide on a shared parent (an empty id would collapse
        # ``work_dir / subdir / ""`` to ``work_dir / subdir``).
        if (
            not workspace_id
            or "/" in workspace_id
            or "\\" in workspace_id
            or workspace_id in (".", "..")
        ):
            raise ValueError(f"Invalid workspace_id for askpass materialization: {workspace_id!r}")
        script_dir = self.work_dir / _SECRET_LEASE_MATERIALIZATION_SUBDIR / workspace_id
        script_dir.mkdir(parents=True, exist_ok=True)
        askpass_path = script_dir / _BITBUCKET_ASKPASS_FILENAME
        # Idempotent re-materialization (reprovision / stack relaunch): a prior
        # run leaves the script at 0o555 with no write bits, so ``write_text``
        # would raise ``PermissionError`` when not running as root. Drop any
        # existing file first so the rewrite always succeeds.
        askpass_path.unlink(missing_ok=True)
        askpass_path.write_text(bitbucket_askpass_script(token_env_var), encoding="utf-8")
        # World read+execute: a non-secret, static script. Read-only bind mounts
        # preserve the host inode mode, so world-x lets the agent execute it
        # without a chown.
        askpass_path.chmod(0o555)
        mounts.append(
            AuthMount(source=str(askpass_path), target=_BITBUCKET_ASKPASS_TARGET, mode="ro")
        )
        apply_bitbucket_agent_git_auth(env, askpass_path=_BITBUCKET_ASKPASS_TARGET)

    def _resolve_local_file_secret(
        self,
        secret: ProfileSecret,
        *,
        provider: str,
        omitted_optional: list[dict[str, str]],
    ) -> AuthMount | None:
        if secret.kind != "mount":
            self._raise(SECRET_LEASE_TARGET_KIND_MISMATCH, secret, provider=provider)
        self._validate_read_only_mount(secret, provider=provider)
        source = self._local_file_source(secret, provider=provider)
        if not source.exists():
            self._missing(secret, provider=provider, omitted_optional=omitted_optional)
            return None
        if not source.is_file():
            self._raise(SECRET_LEASE_SOURCE_INVALID, secret, provider=provider)
        return AuthMount(source=str(source), target=secret.target, mode="ro")

    def _resolve_local_auth_secret(
        self,
        secret: ProfileSecret,
        *,
        provider: str,
        omitted_optional: list[dict[str, str]],
    ) -> AuthMount | None:
        if secret.kind != "mount":
            self._raise(SECRET_LEASE_TARGET_KIND_MISMATCH, secret, provider=provider)
        self._validate_read_only_mount(secret, provider=provider)
        relative_source, expected_target = self._local_auth_source_and_target(
            secret,
            provider=provider,
        )
        if secret.target != expected_target and not _is_safe_secret_target(secret.target):
            self._raise(SECRET_LEASE_TARGET_MISMATCH, secret, provider=provider)
        source = self.host_home.expanduser() / relative_source
        if not source.exists():
            self._missing(secret, provider=provider, omitted_optional=omitted_optional)
            return None
        return AuthMount(source=str(source), target=secret.target, mode="ro")

    def _validate_read_only_mount(self, secret: ProfileSecret, *, provider: str) -> None:
        if secret.mode != "ro":
            self._raise(SECRET_LEASE_WRITABLE_UNSUPPORTED, secret, provider=provider)

    def _local_file_source(self, secret: ProfileSecret, *, provider: str) -> Path:
        ref = (secret.ref or "").strip()
        if not ref:
            self._raise(SECRET_LEASE_SOURCE_INVALID, secret, provider=provider)
        if _source_ref_is_too_broad(ref):
            self._raise(SECRET_LEASE_SOURCE_TOO_BROAD, secret, provider=provider)
        source = Path(ref).expanduser()
        if not source.is_absolute():
            self._raise(SECRET_LEASE_SOURCE_INVALID, secret, provider=provider)
        return source

    def _local_auth_source_and_target(
        self,
        secret: ProfileSecret,
        *,
        provider: str,
    ) -> tuple[str, str]:
        ref = _local_auth_ref(secret.ref)
        if ref is None:
            self._raise(SECRET_LEASE_SOURCE_INVALID, secret, provider=provider)
        if _source_ref_is_too_broad(ref):
            self._raise(SECRET_LEASE_SOURCE_TOO_BROAD, secret, provider=provider)
        known = _KNOWN_LOCAL_AUTH_REFS.get(ref)
        if known is None:
            self._raise(SECRET_LEASE_SOURCE_INVALID, secret, provider=provider)
        return known

    def _missing(
        self,
        secret: ProfileSecret,
        *,
        provider: str,
        omitted_optional: list[dict[str, str]],
    ) -> None:
        if secret.required:
            self._raise(SECRET_LEASE_SOURCE_MISSING, secret, provider=provider)
        omitted_optional.append(
            {
                "secret_name": secret.name,
                "provider": provider,
                "target": secret.target,
                "kind": secret.kind,
                "reason_code": SECRET_LEASE_SOURCE_MISSING,
            }
        )

    def _raise(
        self,
        reason_code: str,
        secret: ProfileSecret,
        *,
        provider: str | None,
    ) -> NoReturn:
        raise SecretLeaseResolutionError(
            reason_code=reason_code,
            secret_name=secret.name,
            provider=provider,
            target=secret.target,
            kind=secret.kind,
        )


def _normalized_provider(secret: ProfileSecret) -> str | None:
    if secret.provider is None and secret.ref is None:
        return None
    if secret.provider is None:
        return None
    provider = secret.provider.strip().lower()
    return provider or None


def _env_ref_name(ref: str | None) -> str | None:
    if ref is None:
        return None
    stripped = ref.strip()
    if stripped.startswith("env/"):
        stripped = stripped[len("env/") :]
    if not _ENV_NAME_RE.fullmatch(stripped):
        return None
    return stripped


def _local_auth_ref(ref: str | None) -> str | None:
    if ref is None:
        return None
    stripped = ref.strip().strip("/")
    for prefix in ("local-auth/", "auth/"):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix) :]
            break
    return stripped or None


def _source_ref_is_too_broad(ref: str) -> bool:
    stripped = ref.strip().rstrip("/")
    if stripped in {
        "~",
        "$HOME",
        "${HOME}",
        "$AWF_HOST_HOME",
        "${AWF_HOST_HOME}",
        "/home",
        "/Users",
    }:
        return True
    if stripped.startswith(("~/", "$HOME/", "${HOME}/", "$AWF_HOST_HOME/", "${AWF_HOST_HOME}/")):
        return True
    return bool(_HOME_ROOT_RE.fullmatch(stripped))


def _is_safe_secret_target(target: str) -> bool:
    return target.startswith(_SAFE_SECRET_MOUNT_PREFIXES)


def _known_local_auth_targets() -> frozenset[str]:
    return frozenset(target for _, target in _KNOWN_LOCAL_AUTH_REFS.values())


def _append_env_pair(env: dict[str, str], pair: tuple[str, str]) -> None:
    key, value = pair
    if key not in env:
        env[key] = value


def _append_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)
