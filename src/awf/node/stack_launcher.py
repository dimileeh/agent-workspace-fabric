"""Launch per-workspace service stacks from resolved workspace profiles."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import posixpath
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from awf.adapters.opencode import opencode_provider_for_model
from awf.common.git_auth import apply_bitbucket_agent_git_auth
from awf.common.git_identity import DEFAULT_GIT_AUTHOR_EMAIL, DEFAULT_GIT_AUTHOR_NAME
from awf.db.enums import AgentRuntime
from awf.node.auth_mounts import WorkspaceAuthMountResolver, legacy_provider_targets
from awf.node.companion_images import CompanionImageBuilder
from awf.node.companion_services import (
    MaterializedCompanionService,
    _hosted_companion_service_from_materialized,
    companion_env_secret_stack_metadata,
    companion_service_from_materialized,
    validate_companion_service_graph,
)
from awf.node.compose_manager import (
    AuthMount,
    CompanionService,
    ComposeManager,
    ComposeOperationError,
    ComposeProjectPaths,
    WorkspaceComposeSpec,
    compose_up_capture_timeout_seconds,
)
from awf.node.egress_policy import local_egress_plan
from awf.node.git_manager import WorktreeLayout
from awf.node.secret_mounts import (
    SECRET_LEASE_PROVIDER_UNSUPPORTED,
    SECRET_LEASE_SOURCE_INVALID,
    SECRET_LEASE_TARGET_KIND_MISMATCH,
    SECRET_LEASE_TARGET_MISMATCH,
    LocalSecretLeaseResolution,
    SecretLeaseResolutionError,
)
from awf.node.stack_launcher_auth_helpers import (
    aws_profile_credential_mounts as _clarification_aws_profile_credential_mounts,
)
from awf.node.stack_launcher_auth_helpers import (
    aws_profile_path_rewrites as _clarification_aws_profile_path_rewrites,
)
from awf.node.stack_launcher_auth_helpers import (
    clarification_auth_target as _helper_clarification_auth_target,
)
from awf.node.stack_launcher_auth_helpers import (
    external_account_subject_token_file_rewrites as _clarification_external_account_subject_token_file_rewrites,
)
from awf.node.stack_launcher_auth_helpers import (
    external_account_subject_token_mounts as _clarification_external_account_subject_token_mounts,
)
from awf.node.stack_launcher_auth_helpers import (
    path_is_below as _clarification_path_is_below,
)
from awf.node.stack_launcher_auth_helpers import (
    provider_auth_mounts as _selected_provider_auth_mounts,
)
from awf.node.stack_launcher_auth_helpers import (
    staged_auth_value as _clarification_staged_auth_value,
)
from awf.node.stack_launcher_auth_helpers import (
    staged_provider_auth_mounts as _clarification_staged_provider_auth_mounts,
)
from awf.node.stack_launcher_compose_helpers import (
    WorkspaceServiceExecutionError as WorkspaceServiceExecutionError,
)
from awf.node.stack_launcher_compose_helpers import (
    _companion_compose_up_timeout_seconds as _companion_compose_up_timeout_seconds,
)
from awf.node.stack_launcher_compose_helpers import (
    _compose_up_reports_missing_image as _compose_up_reports_missing_image,
)
from awf.node.stack_launcher_compose_helpers import (
    _missing_prebuilt_companion_image_retry_spec,
    _prebuilt_companion_image_count,
    _raise_workspace_service_error_if_docker_unavailable,
)
from awf.node.stack_launcher_compose_helpers import (
    effective_compose_up_timeout_seconds as effective_compose_up_timeout_seconds,
)
from awf.profiles.compose import (
    agent_environment_with_declared_secret_leases,
    agent_environment_with_host_auth,
    profile_agent_environment,
    profile_services,
)
from awf.profiles.compose_env import hosted_env_secret_alias_placeholder
from awf.profiles.models import ProfileSecret, WorkspaceProfile
from awf.service.environment import compose_expand_value

_HOSTED_RENDER_ENV_SECRET_PROVIDERS = frozenset(("env", "github", "bitbucket"))
_HOSTED_RENDER_MOUNT_SECRET_PROVIDERS = frozenset(("local-file", "host-file", "local-auth", "auth"))
_HOSTED_RENDER_SECRET_PROVIDERS = (
    _HOSTED_RENDER_ENV_SECRET_PROVIDERS | _HOSTED_RENDER_MOUNT_SECRET_PROVIDERS
)
_HOSTED_RENDER_ENV_REF_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HOSTED_GITHUB_ENV_SOURCE_NAME = "AWF_GITHUB_TOKEN"
_HOSTED_GITHUB_ENV_TARGET_NAMES = ("GH_TOKEN", "GITHUB_TOKEN")
_HOSTED_BITBUCKET_ENV_TARGET_SOURCE_NAMES = {
    "BITBUCKET_API_TOKEN": "BITBUCKET_API_TOKEN",
    "BITBUCKET_EMAIL": "BITBUCKET_EMAIL",
}
_HOSTED_BITBUCKET_ASKPASS_TARGET = "/run/awf/secrets/bb-askpass.sh"
_HOSTED_AUTH_PLACEHOLDER_SOURCE_ROOT = "/run/awf/hosted-auth-placeholders"
_HOSTED_LEGACY_FILE_AUTH_MOUNT_TARGETS = (
    "/home/agent/.claude",
    "/home/agent/.claude.json",
    "/home/agent/.codex",
    "/home/agent/.config/gh",
    "/home/agent/.config/gcloud",
    "/home/agent/.config/opencode",
    "/home/agent/.gemini",
    "/home/agent/.gitconfig",
    "/home/agent/.grok",
    "/home/agent/.ollama",
    "/home/agent/.ssh",
)
_GOOGLE_APPLICATION_CREDENTIALS = "GOOGLE_APPLICATION_CREDENTIALS"
_GOOGLE_APPLICATION_CREDENTIALS_DEFAULT_ADC_TARGET = (
    "/home/agent/.config/gcloud/application_default_credentials.json"
)
_GOOGLE_APPLICATION_CREDENTIALS_DEFAULTED_TARGET_RE = re.compile(
    r"^\$\{GOOGLE_APPLICATION_CREDENTIALS(?::-|-)(?P<target>/[^$}]+)\}$"
)
_AWS_WEB_IDENTITY_TOKEN_FILE = "AWS_WEB_IDENTITY_TOKEN_FILE"
_AWS_WEB_IDENTITY_TOKEN_FILE_DEFAULTED_TARGET_RE = re.compile(
    r"^\$\{AWS_WEB_IDENTITY_TOKEN_FILE(?::-|-)(?P<target>/[^$}]+)\}$"
)
_GCLOUD_AUTH_MOUNT_TARGET = "/home/agent/.config/gcloud"
_CLARIFICATION_GIT_AUTH_MOUNT_TARGETS = frozenset(
    {
        "/home/agent/.config/gh",
        "/home/agent/.gitconfig",
        "/home/agent/.ssh",
        "/run/awf/secrets/bb-askpass.sh",
    }
)
_CLARIFICATION_GIT_AUTH_ENV_PREFIXES = ("GIT_", "GH_", "GITHUB_", "BITBUCKET_")
_CLARIFICATION_CODEX_CREDENTIAL_ENV_NAMES = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_API_TOKEN",
        "CODEX_API_KEY",
        "CODEX_AUTH_TOKEN",
    }
)
_CLARIFICATION_CLAUDE_CODE_CREDENTIAL_ENV_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    }
)
_CLARIFICATION_CLAUDE_CODE_DIRECT_ENV_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
    }
)
_CLARIFICATION_GEMINI_ENV_NAMES: dict[str, frozenset[str]] = {
    "api_key": frozenset(
        {
            "GEMINI_API_KEY",
            "GEMINI_API_KEY_AUTH_MECHANISM",
            "GOOGLE_API_KEY",
        }
    ),
    "google_cloud": frozenset(
        {
            "GOOGLE_GENAI_USE_VERTEXAI",
            "GOOGLE_GENAI_USE_GCA",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_LOCATION",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES",
        }
    ),
    "access_token": frozenset(
        {
            "GOOGLE_CLOUD_ACCESS_TOKEN",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_LOCATION",
        }
    ),
    "file": frozenset(),
}
_CLARIFICATION_GEMINI_AUTH_MOUNT_TARGETS: dict[str, frozenset[str]] = {
    "api_key": frozenset(),
    "google_cloud": frozenset({"/home/agent/.config/gcloud"}),
    "access_token": frozenset(),
    "file": frozenset({"/home/agent/.gemini"}),
}
_CLARIFICATION_RUNTIME_ENV_NAMES: dict[AgentRuntime, frozenset[str]] = {
    AgentRuntime.codex: frozenset(
        {
            "OPENAI_BASE_URL",
            "OPENAI_ORG_ID",
            "OPENAI_ORGANIZATION",
            "OPENAI_PROJECT",
            "OPENAI_PROJECT_ID",
        }
    )
    | _CLARIFICATION_CODEX_CREDENTIAL_ENV_NAMES,
    AgentRuntime.claude_code: _CLARIFICATION_CLAUDE_CODE_DIRECT_ENV_NAMES,
    AgentRuntime.cursor: frozenset({"CURSOR_API_KEY"}),
    AgentRuntime.gemini: frozenset(),
    AgentRuntime.opencode: frozenset(
        {
            "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS",
        }
    ),
    AgentRuntime.grok: frozenset({"XAI_API_KEY"}),
}
_CLARIFICATION_OPENCODE_PROVIDER_ENV_NAMES: dict[str, frozenset[str]] = {
    "ollama": frozenset(
        {
            "AWF_OPENCODE_OLLAMA_BASE_URL",
            "OLLAMA_HOST",
            "OLLAMA_API_KEY",
        }
    ),
    "openai": frozenset({"OPENAI_API_KEY", "OPENAI_BASE_URL"}),
    "anthropic": frozenset({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"}),
    "gemini": frozenset({"GEMINI_API_KEY", "GOOGLE_API_KEY"}),
    "google": frozenset({"GEMINI_API_KEY", "GOOGLE_API_KEY"}),
    "xai": frozenset({"XAI_API_KEY"}),
}
_CLARIFICATION_OPENCODE_PROVIDER_CREDENTIAL_ENV_NAMES: dict[str, frozenset[str]] = {
    "openai": frozenset({"OPENAI_API_KEY"}),
    "anthropic": frozenset({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}),
    "gemini": frozenset({"GEMINI_API_KEY", "GOOGLE_API_KEY"}),
    "google": frozenset({"GEMINI_API_KEY", "GOOGLE_API_KEY"}),
    "xai": frozenset({"XAI_API_KEY"}),
}
_CLARIFICATION_CLAUDE_CODE_BEDROCK_REGION_ENV_NAMES = frozenset(
    {"AWS_REGION", "AWS_DEFAULT_REGION"}
)
_CLARIFICATION_CLAUDE_CODE_BEDROCK_BEARER_TOKEN_ENV_NAMES = frozenset({"AWS_BEARER_TOKEN_BEDROCK"})
_CLARIFICATION_CLAUDE_CODE_BEDROCK_STATIC_CREDENTIAL_ENV_NAMES = frozenset(
    {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}
)
_CLARIFICATION_CLAUDE_CODE_BEDROCK_PROFILE_ENV_NAMES = frozenset(
    {"AWS_PROFILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE"}
)
_CLARIFICATION_AWS_PROFILE_FILE_ENV_NAMES = frozenset(
    {"AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE"}
)
_CLARIFICATION_CLAUDE_CODE_BEDROCK_WEB_IDENTITY_ENV_NAMES = frozenset(
    {"AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE"}
)
_CLARIFICATION_CLAUDE_CODE_VERTEX_ENV_NAMES = frozenset(
    {
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLOUD_ML_REGION",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES",
    }
)
_CLARIFICATION_RUNTIME_AUTH_MOUNT_TARGETS: dict[AgentRuntime, frozenset[str]] = {
    AgentRuntime.codex: frozenset({"/home/agent/.codex"}),
    AgentRuntime.claude_code: frozenset({"/home/agent/.claude", "/home/agent/.claude.json"}),
    AgentRuntime.cursor: frozenset(),
    AgentRuntime.gemini: frozenset({"/home/agent/.config/gcloud", "/home/agent/.gemini"}),
    AgentRuntime.opencode: frozenset(),
    AgentRuntime.grok: frozenset({"/home/agent/.grok"}),
}


def _clarification_agent_environment(
    agent_environment: tuple[tuple[str, str], ...],
    *,
    auth_mounts: Sequence[AuthMount],
    mirror_target: str,
    agent_runtime: AgentRuntime,
    agent_model: str | None = None,
    prefer_file_auth: bool = True,
) -> tuple[tuple[str, str], ...]:
    """Keep model-provider settings and rewrite staged file references.

    Persisted legacy stacks retain direct provider credentials as a fallback:
    their existing file-auth mount cannot be re-resolved before clarification
    starts. Freshly rendered stacks prefer the staged file-auth source.
    """

    agent_environment = _clarification_resolve_google_credentials_placeholder(
        agent_environment,
        auth_mounts=auth_mounts,
        agent_runtime=agent_runtime,
    )
    agent_environment = _clarification_resolve_aws_web_identity_token_file_placeholder(
        agent_environment,
        auth_mounts=auth_mounts,
        agent_runtime=agent_runtime,
    )
    provider_environment_names = _clarification_model_provider_environment_names(
        agent_environment,
        agent_runtime=agent_runtime,
        agent_model=agent_model,
    )
    source_mounts = _clarification_provider_auth_mounts(
        auth_mounts,
        agent_environment=agent_environment,
        mirror_target=mirror_target,
        agent_runtime=agent_runtime,
        agent_model=agent_model,
        provider_environment_names=provider_environment_names,
    )
    staged_mounts = _clarification_staged_provider_auth_mounts(source_mounts)
    staged_targets = tuple(
        (source.target, staged.target)
        for source, staged in zip(source_mounts, staged_mounts, strict=True)
        if source.target != staged.target
    )
    clarification_environment_names = provider_environment_names
    if (
        prefer_file_auth
        and agent_runtime is AgentRuntime.codex
        and _clarification_has_codex_file_auth(source_mounts)
    ):
        # Match Codex readiness: an isolated file-auth mount is preferred to
        # static environment credentials, while non-secret endpoint settings
        # remain available to the clarification re-ask.
        clarification_environment_names -= _CLARIFICATION_CODEX_CREDENTIAL_ENV_NAMES
    if (
        prefer_file_auth
        and agent_runtime is AgentRuntime.claude_code
        and _clarification_has_claude_code_file_auth(source_mounts)
    ):
        # Match Claude readiness: an isolated file-auth mount is preferred to
        # static environment credentials, while non-secret endpoint settings
        # remain available to the clarification re-ask.
        clarification_environment_names -= _CLARIFICATION_CLAUDE_CODE_CREDENTIAL_ENV_NAMES

    return tuple(
        (
            name,
            _clarification_staged_auth_value(
                _clarification_expanded_aws_profile_file_value(name, value), staged_targets
            ),
        )
        for name, value in agent_environment
        if name in clarification_environment_names
        and not _is_clarification_git_auth_environment(name)
    )


def _clarification_has_codex_file_auth(source_mounts: Sequence[AuthMount]) -> bool:
    """Return whether a staged Codex home supplies the file-auth credential."""

    for mount in source_mounts:
        if mount.target not in _CLARIFICATION_RUNTIME_AUTH_MOUNT_TARGETS[AgentRuntime.codex]:
            continue
        try:
            auth = json.loads((Path(mount.source) / "auth.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if _clarification_has_codex_auth_credential(auth):
            return True
    return False


def _clarification_has_codex_auth_credential(auth: object) -> bool:
    """Return whether an auth.json payload contains a Codex credential."""

    if not isinstance(auth, dict):
        return False
    if isinstance(api_key := auth.get("OPENAI_API_KEY"), str) and api_key.strip():
        return True
    tokens = auth.get("tokens")
    return (
        isinstance(tokens, dict)
        and all(
            isinstance(value := tokens.get(name), str) and value.strip()
            for name in ("id_token", "access_token", "refresh_token")
        )
        and _clarification_has_codex_id_token(tokens["id_token"])
    )


def _clarification_has_codex_id_token(value: str) -> bool:
    """Return whether a token has the JWT payload Codex requires in auth.json."""

    parts = value.split(".")
    if len(parts) != 3 or not all(parts):
        return False
    try:
        encoded_payload = parts[1] + ("=" * (-len(parts[1]) % 4))
        payload = base64.b64decode(encoded_payload, altchars=b"-_", validate=True)
        return isinstance(json.loads(payload), dict)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return False


def _clarification_has_claude_code_file_auth(source_mounts: Sequence[AuthMount]) -> bool:
    """Return whether a staged Claude home supplies its OAuth credential store."""

    for mount in source_mounts:
        if mount.target != "/home/agent/.claude":
            continue
        try:
            credentials = json.loads(
                (Path(mount.source) / ".credentials.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if _clarification_has_claude_code_auth_credential(credentials):
            return True
    return False


def _clarification_has_claude_code_auth_credential(credentials: object) -> bool:
    """Return whether a Claude credential payload contains an OAuth access token."""

    if not isinstance(credentials, dict):
        return False
    oauth = credentials.get("claudeAiOauth")
    return (
        isinstance(oauth, dict)
        and isinstance(access_token := oauth.get("accessToken"), str)
        and bool(access_token.strip())
    )


def _clarification_expanded_aws_profile_file_value(name: str, value: str) -> str:
    """Resolve Compose expressions in Bedrock profile-file locations."""

    if name in _CLARIFICATION_AWS_PROFILE_FILE_ENV_NAMES:
        return compose_expand_value(value, environ=os.environ)
    return value


def _is_clarification_git_auth_environment(name: str) -> bool:
    """Return whether an environment variable grants Git authentication."""
    normalized = name.upper()
    return normalized == "SSH_AUTH_SOCK" or normalized.startswith(
        _CLARIFICATION_GIT_AUTH_ENV_PREFIXES
    )


def _clarification_model_provider_environment_names(
    agent_environment: tuple[tuple[str, str], ...],
    *,
    agent_runtime: AgentRuntime,
    agent_model: str | None = None,
) -> frozenset[str]:
    """Return selected runtime env names available to a clarification re-ask.

    Claude Code's Bedrock and Vertex toggles select their own credentials and
    settings rather than adding to direct Anthropic authentication. Gemini
    similarly selects one API-key, Google Cloud, access-token, or CLI-file
    source. Keep every other runtime's settings out of clarification.
    """

    environment_values = dict(agent_environment)
    if agent_runtime is AgentRuntime.claude_code:
        return _clarification_claude_code_environment_names(environment_values)
    if agent_runtime is AgentRuntime.gemini:
        return _CLARIFICATION_GEMINI_ENV_NAMES[
            _clarification_gemini_auth_source(environment_values)
        ]

    provider_names = set(_CLARIFICATION_RUNTIME_ENV_NAMES[agent_runtime])
    if agent_runtime is AgentRuntime.opencode:
        provider_names.update(
            _CLARIFICATION_OPENCODE_PROVIDER_ENV_NAMES.get(
                opencode_provider_for_model(agent_model),
                frozenset(),
            )
        )
    return frozenset(provider_names)


def _clarification_claude_code_environment_names(
    environment_values: dict[str, str],
) -> frozenset[str]:
    """Return direct Claude auth or each explicitly enabled managed backend."""

    backend_names = set()
    if _clarification_claude_code_backend_enabled(
        environment_values, backend_name="CLAUDE_CODE_USE_BEDROCK"
    ):
        backend_names.add("CLAUDE_CODE_USE_BEDROCK")
        backend_names.update(
            _clarification_claude_code_bedrock_environment_names(environment_values)
        )
    if _clarification_claude_code_backend_enabled(
        environment_values, backend_name="CLAUDE_CODE_USE_VERTEX"
    ):
        backend_names.add("CLAUDE_CODE_USE_VERTEX")
        backend_names.update(_CLARIFICATION_CLAUDE_CODE_VERTEX_ENV_NAMES)
    return frozenset(backend_names) or _CLARIFICATION_CLAUDE_CODE_DIRECT_ENV_NAMES


def _clarification_claude_code_backend_enabled(
    environment_values: dict[str, str],
    *,
    backend_name: str,
) -> bool:
    """Return whether Compose resolves a Claude managed-backend toggle to ``1``."""

    return compose_expand_value(environment_values.get(backend_name, ""), environ=os.environ) == "1"


def _clarification_claude_code_bedrock_environment_names(
    environment_values: dict[str, str],
) -> frozenset[str]:
    """Return Bedrock settings plus its first usable credential source."""

    environment_values = {
        name: compose_expand_value(value, environ=os.environ)
        for name, value in environment_values.items()
    }
    if environment_values.get("AWS_BEARER_TOKEN_BEDROCK"):
        credential_names = _CLARIFICATION_CLAUDE_CODE_BEDROCK_BEARER_TOKEN_ENV_NAMES
    elif all(
        environment_values.get(name) for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
    ):
        credential_names = _CLARIFICATION_CLAUDE_CODE_BEDROCK_STATIC_CREDENTIAL_ENV_NAMES
    elif all(
        environment_values.get(name)
        for name in _CLARIFICATION_CLAUDE_CODE_BEDROCK_WEB_IDENTITY_ENV_NAMES
    ):
        credential_names = _CLARIFICATION_CLAUDE_CODE_BEDROCK_WEB_IDENTITY_ENV_NAMES
    elif any(
        environment_values.get(name)
        for name in _CLARIFICATION_CLAUDE_CODE_BEDROCK_PROFILE_ENV_NAMES
    ):
        credential_names = _CLARIFICATION_CLAUDE_CODE_BEDROCK_PROFILE_ENV_NAMES
    else:
        credential_names = frozenset()
    return _CLARIFICATION_CLAUDE_CODE_BEDROCK_REGION_ENV_NAMES | credential_names


def _clarification_gemini_auth_source(environment_values: dict[str, str]) -> str:
    """Return the single Gemini credential source selected for clarification."""

    mechanism = compose_expand_value(
        environment_values.get("GEMINI_API_KEY_AUTH_MECHANISM", ""), environ=os.environ
    ).lower()
    if mechanism in {"api", "api-key", "api_key"}:
        return "api_key"
    if compose_expand_value(
        environment_values.get("GOOGLE_CLOUD_ACCESS_TOKEN", ""), environ=os.environ
    ):
        return "access_token"
    if any(
        compose_expand_value(environment_values.get(name, ""), environ=os.environ).lower()
        in {"1", "true", "yes"}
        for name in ("GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_GCA")
    ):
        return "google_cloud"
    if any(
        compose_expand_value(environment_values.get(name, ""), environ=os.environ)
        for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY")
    ):
        return "api_key"
    return "file"


def _google_credentials_are_within_gcloud_auth_mount(google_credentials: str) -> bool:
    """Return whether a normalized Google credential path is below gcloud auth."""

    return _clarification_path_is_below(google_credentials, _GCLOUD_AUTH_MOUNT_TARGET)


def _clarification_auth_mounts(
    auth_mounts: Sequence[AuthMount],
    *,
    agent_environment: tuple[tuple[str, str], ...],
    mirror_target: str,
    agent_runtime: AgentRuntime,
    agent_model: str | None = None,
) -> tuple[AuthMount, ...]:
    """Return read-only provider sources staged at destinations writable by ``agent``."""

    agent_environment = _clarification_resolve_google_credentials_placeholder(
        agent_environment,
        auth_mounts=auth_mounts,
        agent_runtime=agent_runtime,
    )
    agent_environment = _clarification_resolve_aws_web_identity_token_file_placeholder(
        agent_environment,
        auth_mounts=auth_mounts,
        agent_runtime=agent_runtime,
    )
    provider_environment_names = _clarification_model_provider_environment_names(
        agent_environment,
        agent_runtime=agent_runtime,
        agent_model=agent_model,
    )
    provider_auth_mounts = _clarification_provider_auth_mounts(
        auth_mounts,
        agent_environment=agent_environment,
        mirror_target=mirror_target,
        agent_runtime=agent_runtime,
        agent_model=agent_model,
        provider_environment_names=provider_environment_names,
    )
    return _clarification_staged_provider_auth_mounts(provider_auth_mounts)


def _clarification_resolve_google_credentials_placeholder(
    agent_environment: tuple[tuple[str, str], ...],
    *,
    auth_mounts: Sequence[AuthMount],
    agent_runtime: AgentRuntime,
) -> tuple[tuple[str, str], ...]:
    """Replace a self-referential ADC Compose value with its concrete target."""

    environment_values = dict(agent_environment)
    if agent_runtime is not AgentRuntime.gemini and (
        agent_runtime is not AgentRuntime.claude_code
        or not _clarification_claude_code_backend_enabled(
            environment_values, backend_name="CLAUDE_CODE_USE_VERTEX"
        )
    ):
        return agent_environment
    google_credentials = environment_values.get(_GOOGLE_APPLICATION_CREDENTIALS)
    if google_credentials in (
        f"${{{_GOOGLE_APPLICATION_CREDENTIALS}}}",
        f"${_GOOGLE_APPLICATION_CREDENTIALS}",
    ):
        worker_google_credentials = compose_expand_value(google_credentials, environ=os.environ)
        matching_target = next(
            (
                mount.target
                for mount in auth_mounts
                if mount.mode == "ro"
                and mount.source == worker_google_credentials
                and mount.target == worker_google_credentials
                and mount.target.startswith("/")
            ),
            None,
        )
        if matching_target is not None:
            google_credentials = matching_target
        elif worker_google_credentials:
            return agent_environment
        else:
            dynamic_targets = tuple(
                mount.target
                for mount in auth_mounts
                if mount.mode == "ro"
                and mount.source == mount.target
                and mount.target.startswith("/")
            )
            if len(dynamic_targets) != 1:
                return agent_environment
            google_credentials = dynamic_targets[0]
    elif _GOOGLE_APPLICATION_CREDENTIALS_DEFAULTED_TARGET_RE.fullmatch(google_credentials or ""):
        google_credentials = compose_expand_value(google_credentials or "", environ=os.environ)
    else:
        return agent_environment
    return tuple(
        (name, google_credentials if name == _GOOGLE_APPLICATION_CREDENTIALS else value)
        for name, value in agent_environment
    )


def _clarification_resolve_aws_web_identity_token_file_placeholder(
    agent_environment: tuple[tuple[str, str], ...],
    *,
    auth_mounts: Sequence[AuthMount],
    agent_runtime: AgentRuntime,
) -> tuple[tuple[str, str], ...]:
    """Replace Bedrock web identity Compose values with their concrete targets."""

    environment_values = dict(agent_environment)
    if (
        agent_runtime is not AgentRuntime.claude_code
        or not _clarification_claude_code_backend_enabled(
            environment_values, backend_name="CLAUDE_CODE_USE_BEDROCK"
        )
    ):
        return agent_environment
    token_file = environment_values.get(_AWS_WEB_IDENTITY_TOKEN_FILE)
    if token_file in (
        f"${{{_AWS_WEB_IDENTITY_TOKEN_FILE}}}",
        f"${_AWS_WEB_IDENTITY_TOKEN_FILE}",
    ):
        worker_token_file = compose_expand_value(token_file, environ=os.environ)
        matching_target = next(
            (
                mount.target
                for mount in auth_mounts
                if mount.mode == "ro"
                and mount.target == worker_token_file
                and mount.target.startswith("/")
            ),
            None,
        )
        if matching_target is not None:
            token_file = matching_target
        else:
            dynamic_targets = tuple(
                mount.target
                for mount in auth_mounts
                if mount.mode == "ro"
                and mount.source == mount.target
                and mount.target.startswith("/")
            )
            if len(dynamic_targets) != 1:
                return agent_environment
            token_file = dynamic_targets[0]
    elif _AWS_WEB_IDENTITY_TOKEN_FILE_DEFAULTED_TARGET_RE.fullmatch(token_file or ""):
        token_file = compose_expand_value(token_file or "", environ=os.environ)
    else:
        return agent_environment
    return tuple(
        (name, token_file if name == _AWS_WEB_IDENTITY_TOKEN_FILE else value)
        for name, value in agent_environment
    )


def _clarification_provider_auth_mounts(
    auth_mounts: Sequence[AuthMount],
    *,
    agent_environment: tuple[tuple[str, str], ...],
    mirror_target: str,
    agent_runtime: AgentRuntime,
    agent_model: str | None = None,
    provider_environment_names: frozenset[str] | None = None,
) -> tuple[AuthMount, ...]:
    """Return model-provider authentication mounts available to clarification."""

    provider_environment_names = (
        provider_environment_names
        or _clarification_model_provider_environment_names(
            agent_environment,
            agent_runtime=agent_runtime,
            agent_model=agent_model,
        )
    )
    provider_mount_targets = _clarification_model_provider_auth_mount_targets(
        agent_environment,
        agent_runtime=agent_runtime,
        agent_model=agent_model,
        provider_environment_names=provider_environment_names,
    )
    aws_profile_mount_targets = _clarification_aws_profile_mount_targets(
        agent_environment,
        provider_mount_targets=provider_mount_targets,
    )
    return _selected_provider_auth_mounts(
        auth_mounts,
        provider_mount_targets=provider_mount_targets,
        external_account_subject_token_mounts=_clarification_external_account_subject_token_mounts(
            auth_mounts,
            agent_environment=agent_environment,
            mirror_target=mirror_target,
            provider_environment_names=provider_environment_names,
        ),
        aws_profile_credential_mounts=_clarification_aws_profile_credential_mounts(
            auth_mounts,
            agent_environment=agent_environment,
            provider_mount_targets=aws_profile_mount_targets,
            mirror_target=mirror_target,
        ),
        mirror_target=mirror_target,
    )


def _clarification_aws_profile_mount_targets(
    agent_environment: tuple[tuple[str, str], ...],
    *,
    provider_mount_targets: frozenset[str],
) -> frozenset[str]:
    """Return selected AWS profile-file mount targets, if any."""

    explicit_profile_files = {
        _clarification_expanded_aws_profile_file_value(name, value)
        for name, value in agent_environment
        if name in _CLARIFICATION_AWS_PROFILE_FILE_ENV_NAMES
    }
    return frozenset(
        target
        for target in provider_mount_targets
        if target == "/home/agent/.aws" or target in explicit_profile_files
    )


def _clarification_model_provider_auth_mount_targets(
    agent_environment: tuple[tuple[str, str], ...],
    *,
    agent_runtime: AgentRuntime,
    agent_model: str | None = None,
    provider_environment_names: frozenset[str] | None = None,
) -> frozenset[str]:
    """Return known and environment-referenced model-provider mount targets."""

    names = provider_environment_names or _clarification_model_provider_environment_names(
        agent_environment,
        agent_runtime=agent_runtime,
        agent_model=agent_model,
    )
    runtime_auth_mount_targets = _CLARIFICATION_RUNTIME_AUTH_MOUNT_TARGETS[agent_runtime]
    if (
        agent_runtime is AgentRuntime.claude_code
        and _clarification_claude_code_environment_names(dict(agent_environment))
        != _CLARIFICATION_CLAUDE_CODE_DIRECT_ENV_NAMES
    ):
        environment_values = dict(agent_environment)
        # Stage only the active managed-backend auth stores. Vertex uses the
        # standard gcloud ADC directory only when no explicit credential path
        # is configured. Bedrock profile auth uses the standard AWS directory
        # unless an explicit config or credentials file lies outside the
        # mounted directory.
        runtime_auth_mount_targets = (
            frozenset({_GCLOUD_AUTH_MOUNT_TARGET})
            if _clarification_claude_code_backend_enabled(
                environment_values, backend_name="CLAUDE_CODE_USE_VERTEX"
            )
            and (
                not environment_values.get(_GOOGLE_APPLICATION_CREDENTIALS)
                or _google_credentials_are_within_gcloud_auth_mount(
                    environment_values[_GOOGLE_APPLICATION_CREDENTIALS]
                )
            )
            else frozenset()
        )
        aws_config_file = compose_expand_value(
            environment_values.get("AWS_CONFIG_FILE", ""), environ=os.environ
        )
        normalized_aws_config_file = posixpath.normpath(aws_config_file)
        aws_shared_credentials_file = compose_expand_value(
            environment_values.get("AWS_SHARED_CREDENTIALS_FILE", ""), environ=os.environ
        )
        normalized_aws_shared_credentials_file = posixpath.normpath(aws_shared_credentials_file)
        bedrock_credential_names = _clarification_claude_code_bedrock_environment_names(
            environment_values
        )
        if (
            _clarification_claude_code_backend_enabled(
                environment_values, backend_name="CLAUDE_CODE_USE_BEDROCK"
            )
            # The AWS SDK uses the default profile from ~/.aws when no
            # higher-precedence Bedrock credential source is configured.
            and not (
                bedrock_credential_names
                & (
                    _CLARIFICATION_CLAUDE_CODE_BEDROCK_BEARER_TOKEN_ENV_NAMES
                    | _CLARIFICATION_CLAUDE_CODE_BEDROCK_STATIC_CREDENTIAL_ENV_NAMES
                    | _CLARIFICATION_CLAUDE_CODE_BEDROCK_WEB_IDENTITY_ENV_NAMES
                )
            )
            and (
                not aws_shared_credentials_file
                or normalized_aws_shared_credentials_file.startswith("/home/agent/.aws/")
            )
            and (not aws_config_file or normalized_aws_config_file.startswith("/home/agent/.aws/"))
        ):
            runtime_auth_mount_targets |= frozenset({"/home/agent/.aws"})
    if agent_runtime is AgentRuntime.gemini:
        environment_values = dict(agent_environment)
        gemini_auth_source = _clarification_gemini_auth_source(environment_values)
        runtime_auth_mount_targets = _CLARIFICATION_GEMINI_AUTH_MOUNT_TARGETS[gemini_auth_source]
        google_credentials = environment_values.get(_GOOGLE_APPLICATION_CREDENTIALS)
        if (
            gemini_auth_source == "google_cloud"
            and google_credentials
            and google_credentials != _GOOGLE_APPLICATION_CREDENTIALS_DEFAULT_ADC_TARGET
            and not _google_credentials_are_within_gcloud_auth_mount(google_credentials)
        ):
            # An explicit service-account file takes precedence over ADC.
            runtime_auth_mount_targets = frozenset()
    if agent_runtime is AgentRuntime.grok and any(
        name == "XAI_API_KEY" and compose_expand_value(value, environ=os.environ)
        for name, value in agent_environment
    ):
        # Grok's headless launcher selects an API key before cached-token auth,
        # so do not expose an inactive token store to clarification.
        runtime_auth_mount_targets = frozenset()
    if agent_runtime is AgentRuntime.opencode:
        provider = opencode_provider_for_model(agent_model)
        if provider == "ollama":
            runtime_auth_mount_targets = frozenset({"/home/agent/.ollama"})
        elif not any(
            name in _CLARIFICATION_OPENCODE_PROVIDER_CREDENTIAL_ENV_NAMES.get(provider, frozenset())
            and compose_expand_value(value, environ=os.environ)
            for name, value in agent_environment
        ):
            # Provider readiness permits OpenCode's file auth when no matching
            # provider environment key is present. Stage that fallback only for
            # the selected model, so a direct provider key does not expose the
            # multi-provider OpenCode store to clarification.
            runtime_auth_mount_targets = frozenset({"/home/agent/.config/opencode"})
    return (
        runtime_auth_mount_targets
        | frozenset(
            _clarification_expanded_aws_profile_file_value(name, value)
            for name, value in agent_environment
            if name in names
        )
    ) - _CLARIFICATION_GIT_AUTH_MOUNT_TARGETS


def _clarification_auth_target(target: str, *, index: int) -> str:
    """Keep agent-home targets and stage other paths under the agent home."""

    return _helper_clarification_auth_target(target, index=index)


@dataclass(frozen=True)
class WorkspaceStackLaunchRequest:
    """Inputs required to launch a workspace's outer Compose stack."""

    workspace_id: str
    layout: WorktreeLayout
    profile: WorkspaceProfile
    agent_runtime: AgentRuntime = AgentRuntime.codex
    agent_model: str | None = None
    companions: tuple[MaterializedCompanionService, ...] = ()
    clarification_enabled: bool = True
    """Whether to add AWF's managed clarification service to the rendered stack."""
    companion_graph_prevalidated: bool = False
    on_compose_up_started: Callable[[], Awaitable[None]] | None = None
    """Optional callback invoked when the first compose-up attempt starts."""


class WorkspaceStackLauncher(Protocol):
    """Small seam for provisioning tests to avoid requiring Docker."""

    async def render(self, request: WorkspaceStackLaunchRequest) -> ComposeProjectPaths | None:
        """Render the workspace stack metadata without starting Compose."""
        ...

    async def launch(self, request: WorkspaceStackLaunchRequest) -> ComposeProjectPaths | None:
        """Launch the workspace stack and return rendered compose paths when used."""
        ...


class WorkspaceSecretLeaseResolver(Protocol):
    """Resolves profile-declared local secret leases for the agent container."""

    def resolve(
        self,
        profile: WorkspaceProfile,
        *,
        workspace_id: str,
    ) -> LocalSecretLeaseResolution:
        """Resolve local secret lease mounts and environment for one workspace."""
        ...


class ComposeStackLauncher:
    """Render and start the profile-driven outer workspace Compose stack."""

    def __init__(
        self,
        *,
        compose: ComposeManager,
        agent_runtime_image: str,
        auth_mount_resolver: WorkspaceAuthMountResolver | None = None,
        secret_lease_resolver: WorkspaceSecretLeaseResolver | None = None,
        companion_image_builder: CompanionImageBuilder | None = None,
    ) -> None:
        """Wire stack launch dependencies and optional credential resolvers."""
        self._compose = compose
        self._agent_runtime_image = agent_runtime_image
        self._auth_mount_resolver = auth_mount_resolver
        self._secret_lease_resolver = secret_lease_resolver
        self._companion_image_builder = companion_image_builder

    async def launch(self, request: WorkspaceStackLaunchRequest) -> ComposeProjectPaths | None:
        """Render and start the profile stack, including companions and secret metadata."""
        spec, secret_lease_resolution, companion_secret_metadata = await self._compose_spec(
            request,
            build_companion_images=True,
        )
        try:
            spec = await self._revalidate_prebuilt_companion_images(spec)
        except ComposeOperationError as e:
            _raise_workspace_service_error_if_docker_unavailable(e, spec=spec)
            raise
        missing_prebuilt_retry_budget = _prebuilt_companion_image_count(spec)
        compose_up_started = False

        async def _notify_compose_up_started() -> None:
            nonlocal compose_up_started
            if compose_up_started:
                return
            if request.on_compose_up_started is not None:
                await request.on_compose_up_started()
            compose_up_started = True

        while True:
            try:
                paths = await self._compose.up(
                    spec,
                    wait=True,
                    on_compose_up_started=_notify_compose_up_started,
                )
                break
            except ComposeOperationError as e:
                retry_spec = _missing_prebuilt_companion_image_retry_spec(spec, e)
                if retry_spec is None or missing_prebuilt_retry_budget <= 0:
                    _raise_workspace_service_error_if_docker_unavailable(e, spec=spec)
                    raise
                # Replace spec so retry-time revalidation and any retry failure
                # handling report the compose spec used by the failing attempt.
                spec = retry_spec
                missing_prebuilt_retry_budget -= 1
                try:
                    spec = await self._revalidate_prebuilt_companion_images(spec)
                except ComposeOperationError as retry_error:
                    _raise_workspace_service_error_if_docker_unavailable(
                        retry_error,
                        spec=spec,
                    )
                    raise
        secret_metadata = _stack_secret_metadata(
            secret_lease_resolution=secret_lease_resolution,
            companion_secret_metadata=companion_secret_metadata,
        )
        if not secret_metadata:
            return paths
        return ComposeProjectPaths(
            project_dir=paths.project_dir,
            compose_file=paths.compose_file,
            secret_lease_mount_metadata=secret_metadata,
        )

    async def render(self, request: WorkspaceStackLaunchRequest) -> ComposeProjectPaths | None:
        """Render stack metadata without starting Compose.

        Hosted PR adoption uses this to preserve the same rendered agent
        environment that local Core derives from the stack while intentionally
        skipping local Compose launch. Profile-declared secret leases are kept
        as secret-free names/targets for hosted runtime resolution; render does
        not resolve Core-local lease sources.
        """
        spec, secret_lease_resolution, companion_secret_metadata = await self._compose_spec(
            request,
            build_companion_images=False,
            use_hosted_secret_placeholders=True,
        )
        paths = self._compose.render(spec)
        secret_metadata = _stack_secret_metadata(
            secret_lease_resolution=secret_lease_resolution,
            companion_secret_metadata=companion_secret_metadata,
        )
        if not secret_metadata:
            return paths
        return ComposeProjectPaths(
            project_dir=paths.project_dir,
            compose_file=paths.compose_file,
            secret_lease_mount_metadata=secret_metadata,
        )

    async def _compose_spec(
        self,
        request: WorkspaceStackLaunchRequest,
        *,
        build_companion_images: bool,
        use_hosted_secret_placeholders: bool = False,
    ) -> tuple[WorkspaceComposeSpec, LocalSecretLeaseResolution | None, dict[str, object]]:
        """Build the rendered stack spec shared by launch and render-only paths."""
        layout = request.layout
        profile = request.profile
        egress_plan = local_egress_plan(profile.security.egress)
        # Linked git worktrees store writable refs/objects in the common mirror.
        # Agents need that metadata writable when they make local commits.
        mirror_mount = AuthMount(
            source=str(layout.mirror_path),
            target=str(layout.mirror_path),
            mode="rw",
        )
        auth_mounts = [mirror_mount]
        secret_lease_resolution: LocalSecretLeaseResolution | None = None
        if use_hosted_secret_placeholders:
            secret_lease_resolution = _hosted_secret_lease_placeholder_resolution(profile)
            if secret_lease_resolution is not None:
                auth_mounts.extend(secret_lease_resolution.mounts)
        elif self._secret_lease_resolver is not None:
            secret_lease_resolution = await asyncio.to_thread(
                self._secret_lease_resolver.resolve,
                profile,
                workspace_id=request.workspace_id,
            )
            auth_mounts.extend(secret_lease_resolution.mounts)

        satisfied_targets = (
            secret_lease_resolution.satisfied_legacy_targets
            if secret_lease_resolution is not None
            else frozenset()
        )
        satisfied_providers = (
            secret_lease_resolution.satisfied_legacy_providers
            if secret_lease_resolution is not None
            else frozenset()
        )
        suppressed_legacy_targets = satisfied_targets | legacy_provider_targets(satisfied_providers)
        if use_hosted_secret_placeholders and self._auth_mount_resolver is not None:
            _append_hosted_auth_placeholder_mounts(
                auth_mounts,
                _HOSTED_LEGACY_FILE_AUTH_MOUNT_TARGETS,
                suppressed_targets=suppressed_legacy_targets,
            )
        elif self._auth_mount_resolver is not None:
            legacy_mounts = await asyncio.to_thread(
                self._auth_mount_resolver.resolve,
                workspace_id=request.workspace_id,
                suppressed_targets=satisfied_targets,
                suppressed_providers=satisfied_providers,
            )
            auth_mounts.extend(
                mount for mount in legacy_mounts if mount.target not in suppressed_legacy_targets
            )
        services = await asyncio.to_thread(
            profile_services,
            profile,
            base_path=layout.worktree_path,
        )
        companion_graph_already_validated = (
            bool(request.companions) and request.companion_graph_prevalidated
        )
        if not companion_graph_already_validated:
            await asyncio.to_thread(
                validate_companion_service_graph,
                profile_services=services,
                companions=request.companions,
                docker_mode=profile.docker.mode,
                clarification_enabled=request.clarification_enabled,
            )
        # Give pre-builds and inline builds the same effective compose-up budget.
        compose_up_timeout_seconds = effective_compose_up_timeout_seconds(
            profile=profile,
            companions=request.companions,
        )
        if build_companion_images:
            companions = await self._build_companion_services(
                request.companions,
                capture_timeout_seconds=compose_up_capture_timeout_seconds(
                    compose_up_timeout_seconds, wait=True
                ),
            )
        else:
            companion_service = (
                _hosted_companion_service_from_materialized
                if use_hosted_secret_placeholders
                else companion_service_from_materialized
            )
            companions = tuple(
                await asyncio.gather(
                    *(
                        asyncio.to_thread(companion_service, companion)
                        for companion in request.companions
                    )
                )
            )
        companion_secret_metadata = companion_env_secret_stack_metadata(companions)
        agent_environment = profile_agent_environment(profile)
        if secret_lease_resolution is not None:
            agent_environment = agent_environment_with_declared_secret_leases(
                agent_environment,
                secret_lease_resolution.environment,
            )
        agent_environment = agent_environment_with_host_auth(agent_environment)
        if use_hosted_secret_placeholders and self._auth_mount_resolver is not None:
            _append_hosted_auth_placeholder_mounts(
                auth_mounts,
                _hosted_dynamic_file_auth_mount_targets(agent_environment),
            )
        clarification_auth_mounts = _clarification_auth_mounts(
            auth_mounts,
            agent_environment=agent_environment,
            mirror_target=str(layout.mirror_path),
            agent_runtime=request.agent_runtime,
            agent_model=request.agent_model,
        )
        clarification_external_account_subject_token_file_rewrites = (
            _clarification_external_account_subject_token_file_rewrites(
                auth_mounts,
                agent_environment=agent_environment,
                mirror_target=str(layout.mirror_path),
                agent_runtime=request.agent_runtime,
                agent_model=request.agent_model,
            )
        )
        clarification_aws_profile_path_rewrites = _clarification_aws_profile_path_rewrites(
            auth_mounts,
            agent_environment=agent_environment,
            mirror_target=str(layout.mirror_path),
            agent_runtime=request.agent_runtime,
            agent_model=request.agent_model,
        )
        spec = WorkspaceComposeSpec(
            workspace_id=request.workspace_id,
            worktree_host_path=layout.worktree_path,
            agent_runtime_image=self._agent_runtime_image,
            agent_environment=agent_environment,
            docker_mode=profile.docker.mode.value,
            dind_image=profile.docker.dind_image,
            services=services,
            companions=companions,
            auth_mounts=tuple(auth_mounts),
            clarification_enabled=request.clarification_enabled,
            clarification_agent_environment=_clarification_agent_environment(
                agent_environment,
                auth_mounts=auth_mounts,
                mirror_target=str(layout.mirror_path),
                agent_runtime=request.agent_runtime,
                agent_model=request.agent_model,
            ),
            clarification_auth_mounts=clarification_auth_mounts,
            clarification_external_account_subject_token_file_rewrites=(
                clarification_external_account_subject_token_file_rewrites
            ),
            clarification_aws_profile_path_rewrites=clarification_aws_profile_path_rewrites,
            git_name=DEFAULT_GIT_AUTHOR_NAME,
            git_email=DEFAULT_GIT_AUTHOR_EMAIL,
            network_internal=egress_plan.network_internal,
            host_gateway_enabled=egress_plan.host_gateway_enabled,
            compose_up_timeout_seconds=compose_up_timeout_seconds,
        )
        return spec, secret_lease_resolution, companion_secret_metadata

    async def _build_companion_services(
        self,
        companions: tuple[MaterializedCompanionService, ...],
        *,
        capture_timeout_seconds: float,
    ) -> tuple[CompanionService, ...]:
        """Render companions, pre-building a cached image per companion when possible.

        Each companion is resolved to a Compose service; when an image builder is
        configured it pre-builds (or reuses) a tagged image so the service can
        reference it via ``image:``. A failed or skipped pre-build leaves the
        service as ``build:`` -- identical to the prior behavior.

        Companions are independent, so their pre-builds are dispatched
        concurrently: a multi-companion workspace's provisioning latency is the
        slowest single build rather than the sum of all builds. The builder
        already collapses same-tag waves to one ``docker build`` (see
        :class:`~awf.node.companion_images.CompanionImageBuilder`), so concurrent
        dispatch is safe; ``gather`` preserves input order, keeping the rendered
        services aligned with ``companions``.

        ``capture_timeout_seconds`` is the effective compose-up subprocess cap used
        by the inline ``docker compose up`` build; passing it to the pre-build keeps
        the cache path's build budget aligned with the configured
        ``compose_up_timeout_seconds`` knob so it never times out earlier than the
        inline build it replaces.
        """

        async def _build_single(companion: MaterializedCompanionService) -> CompanionService:
            """Resolve and optionally pre-build one materialized companion service."""
            service = await asyncio.to_thread(companion_service_from_materialized, companion)
            if self._companion_image_builder is not None:
                tag = await self._companion_image_builder.ensure(
                    name=service.name,
                    commit_sha=companion.commit_sha,
                    build_context=service.build_context,
                    dockerfile=service.dockerfile,
                    relative_build_context=companion.spec.build_context,
                    capture_timeout_seconds=capture_timeout_seconds,
                )
                if tag is not None:
                    service = replace(service, image=tag)
            return service

        services = await asyncio.gather(*(_build_single(companion) for companion in companions))
        return tuple(services)

    async def _revalidate_prebuilt_companion_images(
        self,
        spec: WorkspaceComposeSpec,
    ) -> WorkspaceComposeSpec:
        """Clear vanished pre-built companion images so compose builds inline."""
        if self._companion_image_builder is None:
            return spec
        builder = self._companion_image_builder

        async def _revalidate_single(companion: CompanionService) -> CompanionService:
            """Return a build-backed companion when its pre-built image vanished."""
            if companion.image is None:
                return companion
            if await builder.companion_image_exists(companion.image):
                return companion
            return replace(companion, image=None)

        companions = tuple(
            await asyncio.gather(*(_revalidate_single(companion) for companion in spec.companions))
        )
        if companions == spec.companions:
            return spec
        return replace(spec, companions=companions)


def _stack_secret_metadata(
    *,
    secret_lease_resolution: LocalSecretLeaseResolution | None,
    companion_secret_metadata: dict[str, object],
) -> dict[str, object]:
    """Merge profile secret lease metadata with companion env secret metadata."""
    metadata: dict[str, object] = {}
    if secret_lease_resolution is not None:
        metadata.update(dict(secret_lease_resolution.metadata))
    metadata.update(companion_secret_metadata)
    return metadata


def _hosted_secret_lease_placeholder_resolution(
    profile: WorkspaceProfile,
) -> LocalSecretLeaseResolution | None:
    """Return secret-free lease names/targets for hosted render-only stacks."""
    if not profile.secrets:
        return None

    env: dict[str, str] = {}
    lease_env_count = 0
    providers: list[str] = []
    targets: list[str] = []
    mounts: list[AuthMount] = []
    satisfied_legacy_targets: set[str] = set()
    satisfied_legacy_providers: set[str] = set()
    profile_presets_git_askpass = "GIT_ASKPASS" in profile.runtime.environment
    bitbucket_git_token_rendered = False
    mount_count = 0
    skipped_unresolved_count = 0

    for secret in profile.secrets:
        provider = _hosted_secret_provider(secret.provider)
        if provider is None:
            skipped_unresolved_count += 1
            continue
        if provider not in _HOSTED_RENDER_SECRET_PROVIDERS:
            skipped_unresolved_count += _skip_or_raise_unrenderable_hosted_secret(
                secret,
                provider=provider,
                reason_code=SECRET_LEASE_PROVIDER_UNSUPPORTED,
            )
            continue
        if provider in _HOSTED_RENDER_ENV_SECRET_PROVIDERS:
            if secret.kind != "env":
                skipped_unresolved_count += _skip_or_raise_unrenderable_hosted_secret(
                    secret,
                    provider=provider,
                    reason_code=SECRET_LEASE_TARGET_KIND_MISMATCH,
                )
                continue
            pairs = _hosted_env_secret_alias_pairs(secret, provider=provider)
            if pairs is None:
                skipped_unresolved_count += _skip_or_raise_unrenderable_hosted_secret(
                    secret,
                    provider=provider,
                    reason_code=_hosted_env_secret_unrenderable_reason(provider),
                )
                continue
            for target, source_name in pairs:
                injected = target not in env
                if injected:
                    env[target] = hosted_env_secret_alias_placeholder(source_name)
                    lease_env_count += 1
                    if provider == "bitbucket" and target == "BITBUCKET_API_TOKEN":
                        bitbucket_git_token_rendered = True
                _append_unique_hosted_secret_value(targets, target)
            _append_unique_hosted_secret_value(providers, provider)
            if provider == "github":
                satisfied_legacy_providers.add(provider)
            continue
        if secret.kind != "mount":
            skipped_unresolved_count += _skip_or_raise_unrenderable_hosted_secret(
                secret,
                provider=provider,
                reason_code=SECRET_LEASE_TARGET_KIND_MISMATCH,
            )
            continue
        if not secret.target.startswith("/"):
            skipped_unresolved_count += _skip_or_raise_unrenderable_hosted_secret(
                secret,
                provider=provider,
                reason_code=SECRET_LEASE_TARGET_MISMATCH,
            )
            continue
        if provider in _HOSTED_RENDER_MOUNT_SECRET_PROVIDERS:
            mount_count += 1
            _append_unique_hosted_secret_value(providers, provider)
            _append_unique_hosted_secret_value(targets, secret.target)
            _append_hosted_auth_placeholder_mounts(mounts, (secret.target,))
            satisfied_legacy_targets.add(secret.target)
            continue

    if (
        bitbucket_git_token_rendered
        and "GIT_ASKPASS" not in env
        and not profile_presets_git_askpass
        and not any(mount.target == _HOSTED_BITBUCKET_ASKPASS_TARGET for mount in mounts)
    ):
        mount_count += 1
        _append_hosted_auth_placeholder_mounts(mounts, (_HOSTED_BITBUCKET_ASKPASS_TARGET,))
        apply_bitbucket_agent_git_auth(env, askpass_path=_HOSTED_BITBUCKET_ASKPASS_TARGET)

    if not env and mount_count == 0 and not skipped_unresolved_count:
        return None

    metadata: dict[str, object] = {
        "schema": "secret_lease_mount_metadata.v1",
        "mount_plan": "profile_declared_secret_leases",
        "env_count": lease_env_count,
        "mount_count": mount_count,
        "providers": providers,
        "targets": targets,
    }
    if len(env) != lease_env_count:
        metadata["total_env_count"] = len(env)
    if skipped_unresolved_count:
        metadata["skipped_unresolved_count"] = skipped_unresolved_count
    return LocalSecretLeaseResolution(
        environment=tuple(env.items()),
        mounts=tuple(mounts),
        metadata=metadata,
        satisfied_legacy_targets=frozenset(satisfied_legacy_targets),
        satisfied_legacy_providers=frozenset(satisfied_legacy_providers),
    )


def _skip_or_raise_unrenderable_hosted_secret(
    secret: ProfileSecret,
    *,
    provider: str,
    reason_code: str,
) -> int:
    if secret.required:
        raise SecretLeaseResolutionError(
            reason_code=reason_code,
            secret_name=secret.name,
            provider=provider,
            target=secret.target,
            kind=secret.kind,
        )
    return 1


def _hosted_env_secret_unrenderable_reason(provider: str) -> str:
    if provider == "env":
        return SECRET_LEASE_SOURCE_INVALID
    return SECRET_LEASE_TARGET_MISMATCH


def _hosted_secret_provider(provider: str | None) -> str | None:
    if provider is None:
        return None
    normalized = provider.strip().lower()
    return normalized or None


def _hosted_env_secret_alias_pairs(
    secret: ProfileSecret,
    *,
    provider: str,
) -> tuple[tuple[str, str], ...] | None:
    """Return hosted target/source aliases matching local provider lease rules."""
    if provider == "env":
        source_name = _hosted_env_secret_source_name(secret.ref)
        if source_name is None:
            return None
        return ((secret.target, source_name),)
    if provider == "github":
        if secret.target not in _HOSTED_GITHUB_ENV_TARGET_NAMES:
            return None
        return tuple(
            (target, _HOSTED_GITHUB_ENV_SOURCE_NAME) for target in _HOSTED_GITHUB_ENV_TARGET_NAMES
        )
    if provider == "bitbucket":
        source_name = _HOSTED_BITBUCKET_ENV_TARGET_SOURCE_NAMES.get(secret.target)
        if source_name is None:
            return None
        return ((secret.target, source_name),)
    return None


def _hosted_env_secret_source_name(ref: str | None) -> str | None:
    raw = (ref or "").strip()
    if raw.startswith("env/"):
        raw = raw[len("env/") :]
    candidate = raw
    if not _HOSTED_RENDER_ENV_REF_RE.fullmatch(candidate):
        return None
    return candidate


def _append_unique_hosted_secret_value(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def _append_hosted_auth_placeholder_mounts(
    mounts: list[AuthMount],
    targets: Sequence[str],
    *,
    suppressed_targets: frozenset[str] = frozenset(),
) -> None:
    seen = {mount.target for mount in mounts}
    for target in targets:
        if target in seen or target in suppressed_targets or not target.startswith("/"):
            continue
        mounts.append(
            AuthMount(
                source=_hosted_auth_placeholder_source(target),
                target=target,
                mode="ro",
            )
        )
        seen.add(target)


def _hosted_auth_placeholder_source(target: str) -> str:
    name = target.strip("/").replace("/", "__") or "root"
    return f"{_HOSTED_AUTH_PLACEHOLDER_SOURCE_ROOT}/{name}"


def _hosted_dynamic_file_auth_mount_targets(
    agent_environment: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    google_credentials_target = _hosted_google_application_credentials_target(agent_environment)
    if google_credentials_target is None:
        return ()
    return (google_credentials_target,)


def _hosted_google_application_credentials_target(
    agent_environment: tuple[tuple[str, str], ...],
) -> str | None:
    """Return a safe absolute target for a hosted Google ADC mount."""
    raw = dict(agent_environment).get(_GOOGLE_APPLICATION_CREDENTIALS)
    if raw is None:
        return None
    if raw in (
        f"${{{_GOOGLE_APPLICATION_CREDENTIALS}}}",
        f"${_GOOGLE_APPLICATION_CREDENTIALS}",
    ):
        # Hosted render-only cannot resolve executor-local ADC paths from Core.
        return _GOOGLE_APPLICATION_CREDENTIALS_DEFAULT_ADC_TARGET
    if "$" in raw:
        return None
    target = raw
    if not target.startswith("/"):
        return None
    return target
