"""Security linting for resolved workspace profiles."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

from awf.profiles.models import (
    HostHomeAuthMountMode,
    ProfileLintFinding,
    ProfileLintSeverity,
    ProfileSecret,
    WorkspaceProfile,
)

SECRET_TARGET_TOO_BROAD = "SECRET_TARGET_TOO_BROAD"
SECRET_ENV_TARGET_RESERVED = "SECRET_ENV_TARGET_RESERVED"
SECRET_ENV_TARGET_INVALID = "SECRET_ENV_TARGET_INVALID"
SECRET_REF_LOOKS_RAW = "SECRET_REF_LOOKS_RAW"
SECRET_PROVIDER_REF_MISMATCH = "SECRET_PROVIDER_REF_MISMATCH"
HOST_HOME_AUTH_MOUNT_TOO_BROAD = "HOST_HOME_AUTH_MOUNT_TOO_BROAD"
HOST_HOME_AUTH_MOUNT_COMPATIBILITY = "HOST_HOME_AUTH_MOUNT_COMPATIBILITY"
HOST_HOME_AUTH_MOUNT_WRITABLE = "HOST_HOME_AUTH_MOUNT_WRITABLE"
SECRET_LEASE_SOURCE_TOO_BROAD = "SECRET_LEASE_SOURCE_TOO_BROAD"
SECRET_LEASE_WRITABLE_UNSUPPORTED = "SECRET_LEASE_WRITABLE_UNSUPPORTED"

_ENV_TARGET_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED_ENV_TARGETS = frozenset(("HOME", "PATH", "USER"))
_RAW_SECRET_PREFIXES = (
    "sk-",
    "xoxb-",
    "xoxp-",
    "AIza",
    "ghp_",
    "github_pat_",
    "glpat-",
    "hf_",
)
_REFERENCE_RE = re.compile(r"^[A-Za-z0-9_./:@+=,\-?*$]+$")
_JWT_RE = re.compile(r"^[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}$")
_BROAD_SECRET_MOUNT_ROOTS = frozenset(
    (
        "/",
        "/dev",
        "/etc",
        "/home",
        "/proc",
        "/root",
        "/run",
        "/sys",
        "/tmp",
        "/var",
    )
)
_SAFE_SECRET_MOUNT_PREFIXES = ("/run/awf/secrets", "/run/secrets")
_HOST_HOME_PREFIX_PATTERNS = (
    re.compile(r"^\$(?:HOME)(?:/(?P<rest>.*))?$"),
    re.compile(r"^\$(?:AWF_HOST_HOME)(?:/(?P<rest>.*))?$"),
    re.compile(r"^\$\{HOME\}(?:/(?P<rest>.*))?$"),
    re.compile(r"^\$\{AWF_HOST_HOME\}(?:/(?P<rest>.*))?$"),
    re.compile(r"^\$\{AWF_HOST_HOME:-\$\{HOME\}\}(?:/(?P<rest>.*))?$"),
    re.compile(r"^/home/[^/]+(?:/(?P<rest>.*))?$"),
    re.compile(r"^/Users/[^/]+(?:/(?P<rest>.*))?$"),
)
_KNOWN_LOCAL_AUTH_RELS = frozenset(
    (
        ".claude",
        ".claude.json",
        ".codex",
        ".config/gcloud",
        ".config/gh",
        ".config/opencode",
        ".gemini",
        ".gitconfig",
        ".ollama",
        ".ssh",
    )
)
_KNOWN_LOCAL_AUTH_TARGETS = frozenset(
    (
        "/home/agent/.claude",
        "/home/agent/.claude.json",
        "/home/agent/.codex",
        "/home/agent/.config/gcloud",
        "/home/agent/.config/gh",
        "/home/agent/.config/opencode",
        "/home/agent/.gemini",
        "/home/agent/.gitconfig",
        "/home/agent/.ollama",
        "/home/agent/.ssh",
    )
)
_LOCAL_LEASE_SOURCE_PROVIDERS = frozenset(("local-file", "host-file", "local-auth", "auth"))
_LOCAL_AUTH_LEASE_PROVIDERS = frozenset(("local-auth", "auth"))
_AUTH_LIKE_TARGET_SUFFIXES = frozenset(
    (
        ".aws",
        ".claude",
        ".claude.json",
        ".codex",
        ".config/gcloud",
        ".config/gh",
        ".config/opencode",
        ".docker",
        ".gemini",
        ".gitconfig",
        ".gnupg",
        ".kube",
        ".ollama",
        ".ssh",
    )
)


@dataclass(frozen=True)
class _HostHomeSource:
    relative_path: str
    is_root: bool


@dataclass(frozen=True)
class _VolumeTarget:
    path: str
    mode: str


def lint_workspace_profile(profile: WorkspaceProfile) -> tuple[ProfileLintFinding, ...]:
    """Return structured security findings for a fully parsed profile.

    Findings intentionally carry references and paths, not secret values. This
    lets API/console surfaces explain why a profile is unsafe without echoing
    credential material supplied in profile data.
    """

    findings: list[ProfileLintFinding] = []
    for index, secret in enumerate(profile.secrets):
        findings.extend(_lint_secret(secret, index=index))
    findings.extend(lint_profile_service_volumes(profile))
    return tuple(findings)


def profile_lint_errors(profile: WorkspaceProfile) -> tuple[ProfileLintFinding, ...]:
    """Return only blocking profile lint findings."""

    return tuple(
        finding
        for finding in lint_workspace_profile(profile)
        if finding.severity is ProfileLintSeverity.error
    )


def lint_profile_service_volumes(profile: WorkspaceProfile) -> tuple[ProfileLintFinding, ...]:
    """Return profile service-volume lint findings."""

    findings: list[ProfileLintFinding] = []
    for service_index, service in enumerate(profile.services):
        for volume_index, (source, target) in enumerate(service.volumes):
            findings.extend(
                _lint_service_volume(
                    profile,
                    source=source,
                    target=target,
                    path=f"services[{service_index}].volumes[{volume_index}]",
                    service_name=service.name,
                )
            )
    return tuple(findings)


def profile_service_volume_lint_errors(
    profile: WorkspaceProfile,
) -> tuple[ProfileLintFinding, ...]:
    """Return only blocking service-volume lint findings."""

    return tuple(
        finding
        for finding in lint_profile_service_volumes(profile)
        if finding.severity is ProfileLintSeverity.error
    )


def _lint_secret(secret: ProfileSecret, *, index: int) -> tuple[ProfileLintFinding, ...]:
    findings: list[ProfileLintFinding] = []
    path = f"secrets[{index}]"

    if secret.kind == "mount":
        if _declared_local_lease_mount_is_writable(secret):
            findings.append(
                ProfileLintFinding(
                    reason_code=SECRET_LEASE_WRITABLE_UNSUPPORTED,
                    message=(
                        "Profile-declared local secret lease mounts must be read-only; "
                        "use the legacy per-workspace auth seeding path for writable "
                        "compatibility state."
                    ),
                    path=f"{path}.mode",
                    details={
                        "secret_name": secret.name,
                        "kind": secret.kind,
                        "provider": _normalized_secret_provider(secret),
                    },
                )
            )
        if _declared_local_lease_source_is_too_broad(secret):
            findings.append(
                ProfileLintFinding(
                    reason_code=SECRET_LEASE_SOURCE_TOO_BROAD,
                    message=(
                        "Profile-declared local secret lease source is too broad; "
                        "declare exact local files or known local auth refs."
                    ),
                    path=f"{path}.ref",
                    details={
                        "secret_name": secret.name,
                        "kind": secret.kind,
                        "provider": _normalized_secret_provider(secret),
                    },
                )
            )
        if _declared_secret_mount_target_is_too_broad(secret):
            findings.append(
                ProfileLintFinding(
                    reason_code=SECRET_TARGET_TOO_BROAD,
                    message=(
                        "Secret mount target is too broad; prefer "
                        "/run/awf/secrets/<name> for profile-declared secrets."
                    ),
                    path=f"{path}.target",
                    details={"secret_name": secret.name, "kind": secret.kind},
                )
            )
    else:
        findings.extend(_lint_secret_env_target(secret, path=path))

    if bool(secret.provider) != bool(secret.ref):
        findings.append(
            ProfileLintFinding(
                reason_code=SECRET_PROVIDER_REF_MISMATCH,
                message="Secret provider and ref must be declared together.",
                path=path,
                details={"secret_name": secret.name, "kind": secret.kind},
            )
        )

    if _looks_like_raw_secret(secret.ref):
        findings.append(
            ProfileLintFinding(
                reason_code=SECRET_REF_LOOKS_RAW,
                message="Secret ref appears to contain raw secret material.",
                path=f"{path}.ref",
                details={"secret_name": secret.name, "kind": secret.kind},
            )
        )

    return tuple(findings)


def _normalized_secret_provider(secret: ProfileSecret) -> str | None:
    if secret.provider is None:
        return None
    provider = secret.provider.strip().lower()
    return provider or None


def _declared_local_lease_mount_is_writable(secret: ProfileSecret) -> bool:
    provider = _normalized_secret_provider(secret)
    return provider in _LOCAL_LEASE_SOURCE_PROVIDERS and secret.mode != "ro"


def _declared_local_lease_source_is_too_broad(secret: ProfileSecret) -> bool:
    provider = _normalized_secret_provider(secret)
    if provider not in _LOCAL_LEASE_SOURCE_PROVIDERS or secret.ref is None:
        return False
    return _local_lease_source_ref_is_too_broad(secret.ref)


def _declared_secret_mount_target_is_too_broad(secret: ProfileSecret) -> bool:
    return not _declared_local_auth_target_is_allowed(secret) and _secret_mount_target_is_too_broad(
        secret.target
    )


def _declared_local_auth_target_is_allowed(secret: ProfileSecret) -> bool:
    provider = _normalized_secret_provider(secret)
    if provider not in _LOCAL_AUTH_LEASE_PROVIDERS:
        return False
    target = _normalize_container_path(secret.target)
    if target is None:
        return False
    return target in _KNOWN_LOCAL_AUTH_TARGETS


def _local_lease_source_ref_is_too_broad(ref: str) -> bool:
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
    return bool(re.fullmatch(r"/(?:home|Users)/[^/]+", stripped))


def _lint_secret_env_target(secret: ProfileSecret, *, path: str) -> tuple[ProfileLintFinding, ...]:
    target = secret.target.strip()
    if not target or not _ENV_TARGET_RE.fullmatch(target):
        return (
            ProfileLintFinding(
                reason_code=SECRET_ENV_TARGET_INVALID,
                message="Secret env target must be a valid environment variable name.",
                path=f"{path}.target",
                details={"secret_name": secret.name, "kind": secret.kind},
            ),
        )
    if target.upper() in _RESERVED_ENV_TARGETS:
        return (
            ProfileLintFinding(
                reason_code=SECRET_ENV_TARGET_RESERVED,
                message="Secret env target cannot replace reserved runtime variables.",
                path=f"{path}.target",
                details={"secret_name": secret.name, "kind": secret.kind},
            ),
        )
    return ()


def _secret_mount_target_is_too_broad(target: str) -> bool:
    normalized = _normalize_container_path(target)
    if normalized is None:
        return True
    if _contains_parent_reference(target):
        return True
    if any(_path_is_or_under(normalized, prefix) for prefix in _SAFE_SECRET_MOUNT_PREFIXES):
        return False
    return any(_path_is_or_under(normalized, root) for root in _BROAD_SECRET_MOUNT_ROOTS)


def _looks_like_raw_secret(value: str | None) -> bool:
    if value is None:
        return False
    stripped = value.strip()
    if not stripped:
        return False
    if any(stripped.startswith(prefix) for prefix in _RAW_SECRET_PREFIXES):
        return True
    if "-----BEGIN " in stripped or "\n" in stripped:
        return True
    if _JWT_RE.fullmatch(stripped):
        return True
    return len(stripped) > 128 and not bool(_REFERENCE_RE.fullmatch(stripped))


def _lint_service_volume(
    profile: WorkspaceProfile,
    *,
    source: str,
    target: str,
    path: str,
    service_name: str,
) -> tuple[ProfileLintFinding, ...]:
    host_source = _host_home_source(source)
    if host_source is None:
        return ()

    volume_target = _split_volume_target(target)
    target_path = _normalize_container_path(volume_target.path)
    if target_path is None:
        return ()

    details = {"service": service_name, "target": target_path}
    known_credential = (
        host_source.relative_path in _KNOWN_LOCAL_AUTH_RELS
        and target_path in _KNOWN_LOCAL_AUTH_TARGETS
    )
    if known_credential:
        if volume_target.mode != "ro":
            return (
                ProfileLintFinding(
                    reason_code=HOST_HOME_AUTH_MOUNT_WRITABLE,
                    message=(
                        "Known host credential mounts must be read-only unless seeded "
                        "into a per-workspace auth directory."
                    ),
                    path=path,
                    details=details,
                ),
            )
        return (
            ProfileLintFinding(
                reason_code=HOST_HOME_AUTH_MOUNT_COMPATIBILITY,
                message=(
                    "Profile declares a local-development host credential mount; "
                    "declared secrets are preferred."
                ),
                path=path,
                severity=_host_home_policy_severity(profile),
                details=details,
            ),
        )

    if (
        host_source.is_root
        or _is_broad_auth_target(target_path)
        or _is_auth_like_target(target_path)
    ):
        return (
            ProfileLintFinding(
                reason_code=HOST_HOME_AUTH_MOUNT_TOO_BROAD,
                message=(
                    "Profile service volume mounts host-home auth material too broadly; "
                    "use declared secrets or a per-workspace auth directory."
                ),
                path=path,
                details=details,
            ),
        )

    return ()


def _host_home_policy_severity(profile: WorkspaceProfile) -> ProfileLintSeverity:
    mode = profile.security.host_home_auth_mounts.mode
    if mode is HostHomeAuthMountMode.warn:
        return ProfileLintSeverity.warning
    return ProfileLintSeverity.error


def _host_home_source(source: str) -> _HostHomeSource | None:
    stripped = source.strip()
    if stripped == "~":
        return _HostHomeSource(relative_path="", is_root=True)
    if stripped.startswith("~/"):
        return _host_home_relative_source(stripped[2:])

    for pattern in _HOST_HOME_PREFIX_PATTERNS:
        match = pattern.fullmatch(stripped)
        if match is None:
            continue
        return _host_home_relative_source(match.groupdict().get("rest") or "")
    return None


def _host_home_relative_source(rest: str) -> _HostHomeSource:
    normalized = _normalize_relative_path(rest)
    return _HostHomeSource(relative_path=normalized, is_root=normalized == "")


def _normalize_relative_path(value: str) -> str:
    normalized = value.strip().strip("/")
    if not normalized:
        return ""
    return posixpath.normpath(normalized).lstrip("/")


def _split_volume_target(target: str) -> _VolumeTarget:
    stripped = target.strip()
    path, separator, mode_part = stripped.rpartition(":")
    if separator and path.startswith("/"):
        mode_flags = {item.strip() for item in mode_part.split(",") if item.strip()}
        if mode_flags:
            mode = "ro" if "ro" in mode_flags else "rw"
            return _VolumeTarget(path=path, mode=mode)
    return _VolumeTarget(path=target, mode="rw")


def _normalize_container_path(path: str) -> str | None:
    stripped = path.strip()
    if not stripped or not stripped.startswith("/"):
        return None
    normalized = posixpath.normpath(stripped)
    return "/" if normalized == "." else normalized


def _contains_parent_reference(path: str) -> bool:
    return ".." in path.strip().split("/")


def _path_is_or_under(path: str, root: str) -> bool:
    return path == root or path.startswith(f"{root}/")


def _is_broad_auth_target(path: str) -> bool:
    return _path_is_or_under(path, "/home") or _path_is_or_under(path, "/root")


def _is_auth_like_target(path: str) -> bool:
    stripped = path.strip("/")
    return any(
        stripped == suffix or stripped.endswith(f"/{suffix}")
        for suffix in _AUTH_LIKE_TARGET_SUFFIXES
    )
