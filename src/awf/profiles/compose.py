"""Convert workspace profiles into compose-manager inputs."""

from __future__ import annotations

import os
from collections.abc import Mapping

from awf.node.compose_manager import ComposeService
from awf.profiles.models import WorkspaceProfile

AGENT_AUTH_ENV_VARS = (
    # Claude Code portable/API-key auth. Host claude.ai OAuth can live in
    # macOS Keychain, which is not available inside a Linux container.
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    # Gemini CLI headless auth.
    "GEMINI_API_KEY",
    "GEMINI_API_KEY_AUTH_MECHANISM",
    "GOOGLE_API_KEY",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_GENAI_USE_GCA",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_ACCESS_TOKEN",
)


def profile_services(profile: WorkspaceProfile) -> tuple[ComposeService, ...]:
    return tuple(
        ComposeService(
            name=s.name,
            image=s.image,
            build_context=s.build_context,
            dockerfile=s.dockerfile,
            env_file=s.env_file,
            environment=tuple(s.environment.items()),
            depends_on=tuple(s.depends_on),
            healthcheck_cmd=s.healthcheck_cmd,
            ports=tuple(s.ports),
            command=s.command,
            volumes=tuple(s.volumes),
            privileged=s.privileged,
        )
        for s in profile.services
    )


def profile_agent_environment(profile: WorkspaceProfile) -> tuple[tuple[str, str], ...]:
    return tuple(profile.runtime.environment.items())


def agent_environment_with_github_token(
    base_environment: tuple[tuple[str, str], ...],
    *,
    host_env: Mapping[str, str] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Expose AWF's GitHub token to agent containers via Compose placeholders."""
    source_env = os.environ if host_env is None else host_env
    if not source_env.get("AWF_GITHUB_TOKEN"):
        return base_environment

    merged: list[tuple[str, str]] = list(base_environment)
    existing = {key for key, _ in merged}
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if name not in existing:
            merged.append((name, "${AWF_GITHUB_TOKEN}"))
            existing.add(name)
    return tuple(merged)


def agent_environment_with_host_auth(
    base_environment: tuple[tuple[str, str], ...],
    *,
    host_env: Mapping[str, str] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Expose selected host auth env vars to the agent via Compose placeholders.

    Values are intentionally rendered as ``${NAME}`` placeholders. The generated
    per-workspace compose.yml records variable names only; Docker Compose
    substitutes the actual secret values from the worker environment at launch.
    """
    source_env = os.environ if host_env is None else host_env
    merged: list[tuple[str, str]] = list(base_environment)
    existing = {key for key, _ in merged}
    for name in AGENT_AUTH_ENV_VARS:
        if name not in existing and source_env.get(name):
            merged.append((name, f"${{{name}}}"))
            existing.add(name)
    return agent_environment_with_github_token(tuple(merged), host_env=source_env)
