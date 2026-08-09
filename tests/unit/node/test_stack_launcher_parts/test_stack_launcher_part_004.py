"""Stack launcher hosted render-only edge tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.db.enums import AgentRuntime
from awf.node import stack_launcher as stack_launcher_mod
from awf.node.companion_services import (
    CompanionEnvironmentSecretRef,
    MaterializedCompanionService,
    WorkspaceCompanionSpec,
)
from awf.node.compose_manager import AuthMount
from awf.node.git_manager import WorktreeLayout
from awf.node.stack_launcher import ComposeStackLauncher, WorkspaceStackLaunchRequest
from awf.profiles.models import ProfileSecret, WorkspaceProfile
from tests.unit.node.test_stack_launcher_parts._helpers import (
    _FailingDeclaredLeaseResolver,
    _layout,
    _RecordingCompose,
)


@pytest.mark.unit
def test_clarification_inputs_retain_only_selected_adapter_credentials() -> None:
    """Codex clarification cannot read credentials for other coding adapters."""
    mirror = "/host/awf/git/mirrors/repo.git"
    codex_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/codex",
        target="/home/agent/.codex",
        mode="rw",
    )
    provider_credentials = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/google-credentials.json",
        target="/run/awf/secrets/gcp/credentials.json",
        mode="ro",
    )
    aws_shared_credentials = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/aws-credentials",
        target="/run/awf/secrets/aws-credentials",
        mode="ro",
    )
    aws_config = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/aws-config",
        target="/run/awf/secrets/aws-config",
        mode="ro",
    )
    aws_web_identity_token = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/aws-web-identity-token",
        target="/run/awf/secrets/aws-web-identity-token",
        mode="ro",
    )
    database_credentials = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/database-password",
        target="/run/awf/secrets/database-password",
        mode="ro",
    )
    bitbucket_askpass = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/bb-askpass.sh",
        target="/run/awf/secrets/bb-askpass.sh",
        mode="ro",
    )

    auth_mounts = (
        AuthMount(source=mirror, target=mirror, mode="rw"),
        codex_auth,
        provider_credentials,
        aws_shared_credentials,
        aws_config,
        aws_web_identity_token,
        database_credentials,
        bitbucket_askpass,
        AuthMount(
            source="/home/agent/.config/gh",
            target="/home/agent/.config/gh",
            mode="ro",
        ),
        AuthMount(
            source="/home/agent/.gitconfig",
            target="/home/agent/.gitconfig",
            mode="ro",
        ),
        AuthMount(source="/home/agent/.ssh", target="/home/agent/.ssh", mode="ro"),
    )
    agent_environment = (
        ("OPENAI_API_KEY", "${OPENAI_API_KEY}"),
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_ACCESS_KEY_ID", "AKIA_PROFILE_IDENTIFIER"),
        ("AWS_SECRET_ACCESS_KEY", "profile-secret"),
        ("AWS_SESSION_TOKEN", "profile-session-token"),
        ("AWS_REGION", "us-west-2"),
        ("AWS_DEFAULT_REGION", "us-west-2"),
        ("AWS_PROFILE", "awf-bedrock"),
        ("AWS_SHARED_CREDENTIALS_FILE", aws_shared_credentials.target),
        ("AWS_CONFIG_FILE", aws_config.target),
        ("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/awf-bedrock"),
        ("AWS_WEB_IDENTITY_TOKEN_FILE", aws_web_identity_token.target),
        ("AWS_BEARER_TOKEN_BEDROCK", "profile-bedrock-token"),
        ("CLAUDE_CODE_USE_VERTEX", "1"),
        ("ANTHROPIC_VERTEX_PROJECT_ID", "awf-vertex-project"),
        ("CLOUD_ML_REGION", "us-central1"),
        ("GOOGLE_APPLICATION_CREDENTIALS", "/run/awf/secrets/gcp/credentials.json"),
        ("AWF_DATABASE_URL", "postgresql+asyncpg://awf@postgres:5432/awf"),
        ("AWF_TEST_DATABASE_URL", "postgresql+asyncpg://awf@postgres:5432/awf"),
        ("DOCKER_HOST", "tcp://docker:2375"),
        ("SERVICE_TOKEN", "workspace-service-token"),
        ("GIT_ASKPASS", "/run/awf/secrets/bb-askpass.sh"),
        ("GH_TOKEN", "${AWF_GITHUB_TOKEN}"),
        ("GITHUB_TOKEN", "${AWF_GITHUB_TOKEN}"),
        ("BITBUCKET_API_TOKEN", "${BITBUCKET_API_TOKEN}"),
        ("SSH_AUTH_SOCK", "/run/ssh-agent.sock"),
    )
    mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        auth_mounts,
        agent_environment=agent_environment,
        mirror_target=mirror,
        agent_runtime=AgentRuntime.codex,
    )
    environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        agent_environment,
        auth_mounts=auth_mounts,
        mirror_target=mirror,
        agent_runtime=AgentRuntime.codex,
    )

    assert environment == (("OPENAI_API_KEY", "${OPENAI_API_KEY}"),)
    assert mounts == (AuthMount(source=codex_auth.source, target=codex_auth.target, mode="ro"),)


@pytest.mark.unit
def test_clarification_inputs_retain_selected_claude_backend_credentials() -> None:
    """Bedrock and Vertex clarification excludes inactive direct Claude auth."""
    claude_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/claude",
        target="/home/agent/.claude",
        mode="rw",
    )
    aws_credentials = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/aws-credentials",
        target="/run/awf/secrets/aws-credentials",
        mode="ro",
    )
    google_credentials = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/google-credentials.json",
        target="/run/awf/secrets/gcp/credentials.json",
        mode="ro",
    )
    environment = (
        ("OPENAI_API_KEY", "unrelated-openai-token"),
        ("ANTHROPIC_API_KEY", "anthropic-token"),
        ("ANTHROPIC_AUTH_TOKEN", "anthropic-auth-token"),
        ("ANTHROPIC_BASE_URL", "https://anthropic.example.test"),
        ("ANTHROPIC_SMALL_FAST_MODEL", "claude-fast"),
        ("CLAUDE_CODE_OAUTH_TOKEN", "claude-oauth-token"),
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_SECRET_ACCESS_KEY", "bedrock-secret"),
        ("AWS_SHARED_CREDENTIALS_FILE", aws_credentials.target),
        ("CLAUDE_CODE_USE_VERTEX", "1"),
        ("ANTHROPIC_VERTEX_PROJECT_ID", "awf-vertex-project"),
        ("GOOGLE_APPLICATION_CREDENTIALS", google_credentials.target),
    )
    mounts = (claude_auth, aws_credentials, google_credentials)

    clarification_environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        environment,
        auth_mounts=mounts,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )
    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        mounts,
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )

    assert clarification_environment == (
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("AWS_SECRET_ACCESS_KEY", "bedrock-secret"),
        ("AWS_SHARED_CREDENTIALS_FILE", "/home/agent/.awf/clarification-auth/0"),
        ("CLAUDE_CODE_USE_VERTEX", "1"),
        ("ANTHROPIC_VERTEX_PROJECT_ID", "awf-vertex-project"),
        ("GOOGLE_APPLICATION_CREDENTIALS", "/home/agent/.awf/clarification-auth/1"),
    )
    assert clarification_mounts == (
        AuthMount(
            source=aws_credentials.source,
            target="/home/agent/.awf/clarification-auth/0",
            mode="ro",
        ),
        AuthMount(
            source=google_credentials.source,
            target="/home/agent/.awf/clarification-auth/1",
            mode="ro",
        ),
    )


@pytest.mark.unit
def test_clarification_inputs_retain_direct_claude_credentials() -> None:
    """Direct Anthropic clarification retains its credentials and auth mount."""
    claude_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/claude",
        target="/home/agent/.claude",
        mode="rw",
    )
    aws_credentials = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/aws-credentials",
        target="/run/awf/secrets/aws-credentials",
        mode="ro",
    )
    environment = (
        ("ANTHROPIC_API_KEY", "anthropic-token"),
        ("ANTHROPIC_AUTH_TOKEN", "anthropic-auth-token"),
        ("ANTHROPIC_BASE_URL", "https://anthropic.example.test"),
        ("ANTHROPIC_SMALL_FAST_MODEL", "claude-fast"),
        ("CLAUDE_CODE_OAUTH_TOKEN", "claude-oauth-token"),
        ("CLAUDE_CODE_USE_BEDROCK", "0"),
        ("CLAUDE_CODE_USE_VERTEX", "0"),
        ("AWS_SHARED_CREDENTIALS_FILE", aws_credentials.target),
    )

    clarification_environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        environment,
        auth_mounts=(claude_auth, aws_credentials),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )
    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (claude_auth, aws_credentials),
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )

    assert clarification_environment == environment[:-1]
    assert clarification_mounts == (
        AuthMount(source=claude_auth.source, target=claude_auth.target, mode="ro"),
    )


@pytest.mark.unit
def test_opencode_clarification_uses_selected_provider_credentials_only() -> None:
    """A provider-qualified OpenCode re-ask omits shared provider auth stores."""
    opencode_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/opencode",
        target="/home/agent/.config/opencode",
        mode="rw",
    )
    openai_credentials = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/openai-key",
        target="/run/awf/secrets/openai-key",
        mode="ro",
    )
    ollama_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/ollama",
        target="/home/agent/.ollama",
        mode="rw",
    )
    anthropic_credentials = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/anthropic-key",
        target="/run/awf/secrets/anthropic-key",
        mode="ro",
    )
    gemini_credentials = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/gemini-key",
        target="/run/awf/secrets/gemini-key",
        mode="ro",
    )
    xai_credentials = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/xai-key",
        target="/run/awf/secrets/xai-key",
        mode="ro",
    )
    environment = (
        ("OPENAI_API_KEY", openai_credentials.target),
        ("ANTHROPIC_API_KEY", anthropic_credentials.target),
        ("GEMINI_API_KEY", gemini_credentials.target),
        ("XAI_API_KEY", xai_credentials.target),
    )
    mounts = (
        opencode_auth,
        ollama_auth,
        openai_credentials,
        anthropic_credentials,
        gemini_credentials,
        xai_credentials,
    )

    clarification_environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        environment,
        auth_mounts=mounts,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.opencode,
        agent_model="openai/gpt-5",
    )
    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        mounts,
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.opencode,
        agent_model="openai/gpt-5",
    )

    assert clarification_environment == (
        ("OPENAI_API_KEY", "/home/agent/.awf/clarification-auth/0"),
    )
    assert clarification_mounts == (
        AuthMount(
            source=openai_credentials.source,
            target="/home/agent/.awf/clarification-auth/0",
            mode="ro",
        ),
    )


@pytest.mark.unit
def test_opencode_clarification_stages_config_auth_without_provider_environment() -> None:
    """A provider-qualified re-ask retains OpenCode file auth as a fallback."""
    opencode_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/opencode",
        target="/home/agent/.config/opencode",
        mode="rw",
    )

    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (opencode_auth,),
        agent_environment=(),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.opencode,
        agent_model="openai/gpt-5",
    )

    assert clarification_mounts == (
        AuthMount(source=opencode_auth.source, target=opencode_auth.target, mode="ro"),
    )


@pytest.mark.unit
def test_opencode_ollama_clarification_omits_shared_opencode_store() -> None:
    """Ollama re-asks retain Ollama auth without mounting multi-provider config."""
    opencode_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/opencode",
        target="/home/agent/.config/opencode",
        mode="rw",
    )
    ollama_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/ollama",
        target="/home/agent/.ollama",
        mode="rw",
    )
    ollama_credentials = AuthMount(
        source="/host/awf/secret-leases/ws_launcher/ollama-key",
        target="/run/awf/secrets/ollama-key",
        mode="ro",
    )
    environment = (("OLLAMA_API_KEY", ollama_credentials.target),)
    mounts = (opencode_auth, ollama_auth, ollama_credentials)

    clarification_environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        environment,
        auth_mounts=mounts,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.opencode,
        agent_model="ollama/kimi-k2.6:cloud",
    )
    clarification_mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        mounts,
        agent_environment=environment,
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.opencode,
        agent_model="ollama/kimi-k2.6:cloud",
    )

    assert clarification_environment == (
        ("OLLAMA_API_KEY", "/home/agent/.awf/clarification-auth/1"),
    )
    assert clarification_mounts == (
        AuthMount(source=ollama_auth.source, target=ollama_auth.target, mode="ro"),
        AuthMount(
            source=ollama_credentials.source,
            target="/home/agent/.awf/clarification-auth/1",
            mode="ro",
        ),
    )


@pytest.mark.unit
def test_clarification_inputs_exclude_git_mount_selected_by_provider_variable() -> None:
    """A provider environment value cannot reintroduce a Git auth mount."""
    git_auth = AuthMount(
        source="/host/awf/auth/ws_launcher/ssh",
        target="/home/agent/.ssh",
        mode="ro",
    )

    mounts = stack_launcher_mod._clarification_auth_mounts(  # noqa: SLF001
        (git_auth,),
        agent_environment=(("OPENAI_API_KEY", git_auth.target),),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.codex,
    )

    assert mounts == ()


@pytest.mark.unit
def test_clarification_inputs_exclude_unselected_claude_backend_settings() -> None:
    """Claude backend credentials stay out unless that backend is enabled."""
    environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        (
            ("CLAUDE_CODE_USE_BEDROCK", "0"),
            ("AWS_SECRET_ACCESS_KEY", "unselected-backend-secret"),
            ("CLAUDE_CODE_USE_VERTEX", "0"),
            ("ANTHROPIC_VERTEX_PROJECT_ID", "unselected-vertex-project"),
            ("GIT_ASKPASS", "/run/awf/secrets/bb-askpass.sh"),
        ),
        auth_mounts=(),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.claude_code,
    )

    assert environment == (
        ("CLAUDE_CODE_USE_BEDROCK", "0"),
        ("CLAUDE_CODE_USE_VERTEX", "0"),
    )


@pytest.mark.unit
def test_clarification_environment_computes_provider_names_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clarification reuses provider names while staging its selected mounts."""
    calls = 0
    original = stack_launcher_mod._clarification_model_provider_environment_names  # noqa: SLF001

    def _record_provider_names(
        agent_environment: tuple[tuple[str, str], ...],
        *,
        agent_runtime: AgentRuntime,
        agent_model: str | None = None,
    ) -> frozenset[str]:
        nonlocal calls
        calls += 1
        return original(
            agent_environment,
            agent_runtime=agent_runtime,
            agent_model=agent_model,
        )

    monkeypatch.setattr(
        stack_launcher_mod,
        "_clarification_model_provider_environment_names",
        _record_provider_names,
    )

    environment = stack_launcher_mod._clarification_agent_environment(  # noqa: SLF001
        (("OPENAI_API_KEY", "token"),),
        auth_mounts=(),
        mirror_target="/host/awf/git/mirrors/repo.git",
        agent_runtime=AgentRuntime.codex,
    )

    assert environment == (("OPENAI_API_KEY", "token"),)
    assert calls == 1


@pytest.mark.unit
async def test_compose_stack_launcher_launch_notifies_compose_up_started_once() -> None:
    """The compose-start callback fires once when the first compose up starts."""
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )
    starts = 0

    async def _started() -> None:
        nonlocal starts
        starts += 1

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=WorkspaceProfile(name="generic"),
            on_compose_up_started=_started,
        )
    )

    assert starts == 1
    assert compose.waits == [True]


@pytest.mark.unit
async def test_compose_stack_launcher_render_without_profile_secrets_returns_plain_paths() -> None:
    """Hosted render-only stacks with no leases return ordinary compose paths."""
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
        secret_lease_resolver=_FailingDeclaredLeaseResolver(),
    )
    layout = _layout()

    paths = await launcher.render(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=layout,
            profile=WorkspaceProfile(name="hosted-no-secrets"),
        )
    )

    assert paths is not None
    assert paths.secret_lease_mount_metadata == {}
    assert compose.specs == []
    assert len(compose.render_specs) == 1
    assert compose.render_specs[0].auth_mounts == (
        AuthMount(source=str(layout.mirror_path), target=str(layout.mirror_path), mode="rw"),
    )


@pytest.mark.unit
async def test_compose_stack_launcher_render_preserves_companion_env_secret_placeholders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosted render keeps companion env secret source names without local env resolution."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPTIONAL_TOKEN_SOURCE", raising=False)
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )
    companion = MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(
            name="backend",
            repo_url="git@github.com:example/backend.git",
            environment_secrets=(
                CompanionEnvironmentSecretRef(
                    target="AIRA_API_KEY",
                    value_from="ANTHROPIC_API_KEY",
                ),
                CompanionEnvironmentSecretRef(
                    target="OPTIONAL_TOKEN",
                    value_from="OPTIONAL_TOKEN_SOURCE",
                    required=False,
                ),
            ),
        ),
        layout=_layout(),
    )

    paths = await launcher.render(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=WorkspaceProfile(name="hosted-companion-secrets"),
            companions=(companion,),
            companion_graph_prevalidated=True,
        )
    )

    assert paths is not None
    assert compose.specs == []
    assert len(compose.render_specs) == 1
    rendered = compose.render_specs[0].companions[0]
    assert rendered.environment == (
        ("AIRA_API_KEY", "${ANTHROPIC_API_KEY}"),
        ("OPTIONAL_TOKEN", "${OPTIONAL_TOKEN_SOURCE}"),
    )
    assert paths.secret_lease_mount_metadata["companion_env_secret_count"] == 2
    assert paths.secret_lease_mount_metadata["companion_env_secrets"] == (
        {
            "companion": "backend",
            "target": "AIRA_API_KEY",
            "provider": "env",
            "source": "ANTHROPIC_API_KEY",
            "required": True,
        },
        {
            "companion": "backend",
            "target": "OPTIONAL_TOKEN",
            "provider": "env",
            "source": "OPTIONAL_TOKEN_SOURCE",
            "required": False,
        },
    )


@pytest.mark.unit
async def test_compose_stack_launcher_render_attaches_portable_companion_source_metadata(
    tmp_path: Path,
) -> None:
    """Hosted render carries Git/build metadata instead of only Core-local paths."""
    companion_root = tmp_path / "backend"
    (companion_root / "services" / "api").mkdir(parents=True)
    (companion_root / "config").mkdir()
    (companion_root / "fixtures").mkdir()
    (companion_root / "docker").mkdir()
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )
    companion = MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(
            name="backend",
            repo_url="ssh://git:source-token@github.com/example/backend.git",
            base_branch="development",
            build_context="services/api",
            dockerfile="docker/Dockerfile",
            env_file="config/dev.env",
            volumes=(("./fixtures", "/fixtures"), ("cache_data", "/cache")),
        ),
        layout=WorktreeLayout(
            mirror_path=tmp_path / "backend.git",
            worktree_path=companion_root,
            branch_name="awf/ws_launcher/companion/backend",
        ),
        commit_sha="abc123def456",
    )

    await launcher.render(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=WorkspaceProfile(name="hosted-companion-source"),
            companions=(companion,),
            companion_graph_prevalidated=True,
        )
    )

    rendered = compose.render_specs[0].companions[0]
    assert rendered.build_context == str(companion_root / "services" / "api")
    assert rendered.source_metadata == {
        "schema": "hosted_companion_source.v1",
        "name": "backend",
        "repo_url": "ssh://git@github.com/example/backend.git",
        "base_branch": "development",
        "commit_sha": "abc123def456",
        "build_context": "services/api",
        "dockerfile": "docker/Dockerfile",
        "env_file": "config/dev.env",
        "volumes": (
            {"source": "./fixtures", "target": "/fixtures"},
            {"source": "cache_data", "target": "/cache"},
        ),
    }


@pytest.mark.unit
async def test_compose_stack_launcher_render_records_optional_unrenderable_hosted_secrets() -> None:
    """Optional hosted secret declarations are skipped explicitly, not resolved locally."""
    compose = _RecordingCompose()
    lease_resolver = _FailingDeclaredLeaseResolver()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
        secret_lease_resolver=lease_resolver,
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-optional-unrenderable",
            "secrets": [
                {
                    "name": "missing-provider",
                    "kind": "mount",
                    "target": "/home/agent/.missing",
                    "required": False,
                },
                {
                    "name": "blank-provider",
                    "kind": "mount",
                    "target": "/home/agent/.blank",
                    "provider": " ",
                    "required": False,
                },
                {
                    "name": "vault-token",
                    "kind": "env",
                    "target": "VAULT_TOKEN",
                    "provider": "vault",
                    "required": False,
                },
                {
                    "name": "bad-env-ref",
                    "kind": "env",
                    "target": "OPENAI_API_KEY",
                    "provider": "env",
                    "ref": "env/1_OPENAI_API_KEY",
                    "required": False,
                },
                {
                    "name": "github-wrong-target",
                    "kind": "env",
                    "target": "NOT_GITHUB_TOKEN",
                    "provider": "github",
                    "required": False,
                },
                {
                    "name": "github-mount",
                    "kind": "mount",
                    "target": "/home/agent/.config/gh",
                    "provider": "github",
                    "required": False,
                },
                {
                    "name": "bitbucket-wrong-target",
                    "kind": "env",
                    "target": "BB_TOKEN",
                    "provider": "bitbucket",
                    "required": False,
                },
                {
                    "name": "local-file-env",
                    "kind": "env",
                    "target": "NPMRC",
                    "provider": "local-file",
                    "required": False,
                },
                {
                    "name": "relative-local-file",
                    "kind": "mount",
                    "target": "relative-npmrc",
                    "provider": "local-file",
                    "required": False,
                },
            ],
        }
    )

    paths = await launcher.render(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=profile,
        )
    )

    assert paths is not None
    assert lease_resolver.calls == []
    assert compose.specs == []
    assert len(compose.render_specs) == 1
    assert paths.secret_lease_mount_metadata == {
        "schema": "secret_lease_mount_metadata.v1",
        "mount_plan": "profile_declared_secret_leases",
        "env_count": 0,
        "mount_count": 0,
        "providers": [],
        "targets": [],
        "skipped_unresolved_count": 9,
    }


@pytest.mark.unit
async def test_compose_stack_launcher_render_deduplicates_hosted_env_targets() -> None:
    """Duplicate hosted env lease targets keep one rendered target and source."""
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="custom-agent-runtime:dev",
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-duplicate-env",
            "secrets": [
                {
                    "name": "openai-primary",
                    "kind": "env",
                    "target": "OPENAI_API_KEY",
                    "provider": "env",
                    "ref": "env/OPENAI_PRIMARY",
                },
                {
                    "name": "openai-secondary",
                    "kind": "env",
                    "target": "OPENAI_API_KEY",
                    "provider": "env",
                    "ref": "env/OPENAI_SECONDARY",
                },
            ],
        }
    )

    paths = await launcher.render(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_launcher",
            layout=_layout(),
            profile=profile,
        )
    )

    assert paths is not None
    assert paths.secret_lease_mount_metadata["env_count"] == 1
    assert paths.secret_lease_mount_metadata["targets"] == ["OPENAI_API_KEY"]
    assert paths.secret_lease_mount_metadata["providers"] == ["env"]


@pytest.mark.unit
def test_hosted_env_secret_alias_pairs_ignores_unknown_provider() -> None:
    """The alias helper fails closed for a provider outside the hosted allowlist."""
    secret = ProfileSecret(
        name="unknown",
        kind="env",
        target="UNKNOWN_TOKEN",
        provider="unknown",
        ref="env/UNKNOWN_TOKEN",
    )

    assert (
        stack_launcher_mod._hosted_env_secret_alias_pairs(
            secret,
            provider="unknown",
        )
        is None
    )


@pytest.mark.unit
def test_hosted_dynamic_file_auth_mount_targets_accept_only_absolute_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosted ADC file targets preserve metadata without Core-local env resolution."""
    hosted_adc_target = "/home/agent/.config/gcloud/application_default_credentials.json"
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/core/adc/should-not-leak.json")

    assert stack_launcher_mod._hosted_dynamic_file_auth_mount_targets(
        (("GOOGLE_APPLICATION_CREDENTIALS", "${GOOGLE_APPLICATION_CREDENTIALS}"),)
    ) == (hosted_adc_target,)
    assert stack_launcher_mod._hosted_dynamic_file_auth_mount_targets(
        (("GOOGLE_APPLICATION_CREDENTIALS", "/profile/gcp/application.json"),)
    ) == ("/profile/gcp/application.json",)
    assert stack_launcher_mod._hosted_dynamic_file_auth_mount_targets(
        (("GOOGLE_APPLICATION_CREDENTIALS", "$GOOGLE_APPLICATION_CREDENTIALS"),)
    ) == (hosted_adc_target,)
    assert stack_launcher_mod._hosted_dynamic_file_auth_mount_targets(()) == ()
    assert (
        stack_launcher_mod._hosted_dynamic_file_auth_mount_targets(
            (("GOOGLE_APPLICATION_CREDENTIALS", "${OTHER_CREDENTIALS}"),)
        )
        == ()
    )

    assert (
        stack_launcher_mod._hosted_dynamic_file_auth_mount_targets(
            (("GOOGLE_APPLICATION_CREDENTIALS", "relative.json"),)
        )
        == ()
    )
