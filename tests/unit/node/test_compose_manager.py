"""ComposeManager unit tests — template rendering only.

Docker-daemon-dependent tests live under ``tests/integration/`` and are
skipped when a daemon isn't available. These unit tests verify the rendered
compose YAML is syntactically valid and contains all the expected wiring.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf.node.compose_manager import (
    CompanionService,
    ComposeManager,
    ComposeProjectPaths,
    ComposeService,
    WorkspaceComposeSpec,
)
from awf.profiles.registry import docker_compose_profile

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


@pytest.fixture
def manager(tmp_path: Path) -> ComposeManager:
    """Provide a compose manager rooted in the test temp directory."""
    return ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)


def _spec(tmp_path: Path, **overrides: object) -> WorkspaceComposeSpec:
    base = {
        "workspace_id": "ws_test123",
        "worktree_host_path": tmp_path / "worktree",
        "postgres_password": "deterministic-for-test",
    }
    base.update(overrides)
    return WorkspaceComposeSpec(**base)  # type: ignore[arg-type]


@pytest.mark.unit
def test_compose_project_paths_secret_metadata_cannot_be_mutated() -> None:
    """Compose project path secret metadata is immutable after construction."""
    paths = ComposeProjectPaths(
        project_dir=Path("/tmp/compose/ws_secret"),
        compose_file=Path("/tmp/compose/ws_secret/compose.yml"),
        secret_lease_mount_metadata={
            "providers": ["env"],
            "omitted_optional": [{"secret_name": "optional-openai"}],
        },
    )

    with pytest.raises(TypeError):
        paths.secret_lease_mount_metadata["extra"] = "injected"
    with pytest.raises(AttributeError):
        paths.secret_lease_mount_metadata["providers"].append("injected")
    omitted_optional = paths.secret_lease_mount_metadata["omitted_optional"]
    with pytest.raises(TypeError):
        omitted_optional[0]["secret_name"] = "changed"


class TestRender:
    """Tests for rendering workspace compose specifications."""

    @pytest.mark.unit
    def test_renders_valid_yaml(self, manager: ComposeManager, tmp_path: Path) -> None:
        """The rendered compose file is valid YAML with the base services."""
        paths = manager.render(_spec(tmp_path))
        assert paths.compose_file.exists()

        parsed = yaml.safe_load(paths.compose_file.read_text())
        assert set(parsed.keys()) == {"services", "volumes", "networks"}
        assert set(parsed["services"].keys()) == {"agent"}

    @pytest.mark.unit
    def test_mounts_worktree_into_agent_at_workspace(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """The agent container mounts the workspace worktree at /workspace."""
        spec = _spec(tmp_path)
        paths = manager.render(spec)

        parsed = yaml.safe_load(paths.compose_file.read_text())
        volumes = parsed["services"]["agent"]["volumes"]
        assert volumes == [f"{spec.worktree_host_path}:/workspace"]

    @pytest.mark.unit
    def test_agent_can_reach_host_gateway_for_host_services(
        self,
        manager: ComposeManager,
        tmp_path: Path,
    ) -> None:
        """Default rendering exposes the host gateway to the agent."""
        rendered = manager.render(_spec(tmp_path)).compose_file.read_text()
        parsed = yaml.safe_load(rendered)

        assert parsed["services"]["agent"]["extra_hosts"] == ["host.docker.internal:host-gateway"]
        assert "\n    \n    extra_hosts:" not in rendered

    @pytest.mark.unit
    def test_open_egress_policy_keeps_public_network_and_host_gateway(
        self,
        manager: ComposeManager,
        tmp_path: Path,
    ) -> None:
        """Open egress keeps a public network and host gateway mapping."""
        parsed = yaml.safe_load(
            manager.render(
                _spec(tmp_path, network_internal=False, host_gateway_enabled=True)
            ).compose_file.read_text()
        )

        assert "internal" not in parsed["networks"]["awf_net"]
        assert parsed["services"]["agent"]["extra_hosts"] == ["host.docker.internal:host-gateway"]

    @pytest.mark.unit
    def test_offline_egress_policy_renders_internal_network_without_host_gateway(
        self,
        manager: ComposeManager,
        tmp_path: Path,
    ) -> None:
        """Offline egress renders an internal network without host gateway access."""
        parsed = yaml.safe_load(
            manager.render(
                _spec(tmp_path, network_internal=True, host_gateway_enabled=False)
            ).compose_file.read_text()
        )

        assert parsed["networks"]["awf_net"]["internal"] is True
        assert "extra_hosts" not in parsed["services"]["agent"]

    @pytest.mark.unit
    def test_offline_egress_policy_keeps_agent_and_services_on_awf_network(
        self,
        manager: ComposeManager,
        tmp_path: Path,
    ) -> None:
        """Offline egress keeps the agent and profile services on the AWF network."""
        parsed = yaml.safe_load(
            manager.render(
                _spec(
                    tmp_path,
                    network_internal=True,
                    host_gateway_enabled=False,
                    services=(
                        ComposeService(
                            name="mirror",
                            image="registry-mirror:local",
                        ),
                    ),
                )
            ).compose_file.read_text()
        )

        assert parsed["services"]["agent"]["networks"] == ["awf_net"]
        assert parsed["services"]["mirror"]["networks"] == ["awf_net"]

    @pytest.mark.unit
    def test_profile_service_password_placeholders_are_resolved(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """Profile service password placeholders resolve during rendering."""
        from awf.node.compose_manager import ComposeService

        spec = _spec(
            tmp_path,
            postgres_password="my-secret-1234",
            agent_environment=(
                ("DATABASE_URL", "postgresql://awf:${AWF_POSTGRES_PASSWORD}@postgres/awf"),
            ),
            services=(
                ComposeService(
                    name="postgres",
                    image="postgres:16",
                    environment=(("POSTGRES_PASSWORD", "${AWF_POSTGRES_PASSWORD}"),),
                    healthcheck_cmd="pg_isready -U awf",
                    volumes=(("postgres_data", "/var/lib/postgresql/data"),),
                ),
            ),
        )
        paths = manager.render(spec)

        parsed = yaml.safe_load(paths.compose_file.read_text())
        agent_env = parsed["services"]["agent"]["environment"]
        pg_env = parsed["services"]["postgres"]["environment"]

        assert pg_env["POSTGRES_PASSWORD"] == "my-secret-1234"
        assert "my-secret-1234" in agent_env["DATABASE_URL"]
        assert parsed["volumes"]["postgres_data"]["name"] == "awf-ws_test123-postgres_data"

    @pytest.mark.unit
    def test_password_placeholder_expansion_noops_without_postgres_password(
        self, manager: ComposeManager
    ) -> None:
        """Password placeholder expansion is a no-op without a password."""
        assert (
            manager._expand_placeholders(
                "postgresql://awf:${AWF_POSTGRES_PASSWORD}@postgres/awf",
                postgres_password=None,
            )
            == "postgresql://awf:${AWF_POSTGRES_PASSWORD}@postgres/awf"
        )

    @pytest.mark.unit
    def test_companion_service_password_placeholders_are_resolved(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """Companion service password placeholders resolve during rendering."""
        spec = _spec(
            tmp_path,
            postgres_password="companion-secret",
            companions=(
                CompanionService(
                    name="backend",
                    build_context="/host/backend",
                    environment=(
                        (
                            "DATABASE_URL",
                            "postgresql://awf:${AWF_POSTGRES_PASSWORD}@postgres/awf",
                        ),
                    ),
                ),
            ),
        )

        parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())

        assert parsed["services"]["backend"]["environment"] == {
            "DATABASE_URL": "postgresql://awf:companion-secret@postgres/awf"
        }

    @pytest.mark.unit
    def test_dind_companion_environment_secret_placeholder_is_rendered_without_raw_value(
        self,
        manager: ComposeManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """DinD companion secret placeholders render without exposing raw values."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "raw-secret-value")
        spec = _spec(
            tmp_path,
            docker_mode="dind",
            companions=(
                CompanionService(
                    name="backend",
                    build_context="/host/backend",
                    environment=(
                        (
                            "AIRA_API_KEY",
                            "${ANTHROPIC_API_KEY:?COMPANION_ENV_SECRET_SOURCE_MISSING_OR_"
                            "COMPANION_ENV_SECRET_SOURCE_EMPTY: "
                            "companion=backend, target=AIRA_API_KEY, provider=env, "
                            "source=ANTHROPIC_API_KEY}",
                        ),
                    ),
                    depends_on=("docker",),
                    healthcheck_cmd="curl -fsS http://localhost:8000/healthz",
                ),
            ),
        )

        rendered = manager.render(spec).compose_file.read_text()
        parsed = yaml.safe_load(rendered)

        assert parsed["services"]["docker"]["image"] == "docker:27-dind"
        assert parsed["services"]["backend"]["depends_on"] == {
            "docker": {"condition": "service_healthy"}
        }
        assert parsed["services"]["backend"]["environment"] == {
            "AIRA_API_KEY": (
                "${ANTHROPIC_API_KEY:?COMPANION_ENV_SECRET_SOURCE_MISSING_OR_"
                "COMPANION_ENV_SECRET_SOURCE_EMPTY: "
                "companion=backend, target=AIRA_API_KEY, provider=env, "
                "source=ANTHROPIC_API_KEY}"
            )
        }
        assert "raw-secret-value" not in rendered

    @pytest.mark.unit
    def test_profile_service_image_is_escaped_against_yaml_injection(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """A malicious ``image`` value cannot inject sibling compose keys.

        ``ProfileService`` only bounds field length, not its character set, so a
        profile from a project repo could embed quotes/newlines. The template
        must JSON-escape ``image`` (like ``environment``/``command``) so the
        value renders as one scalar instead of breaking out to inject keys such
        as ``privileged``.
        """
        malicious = 'evil:latest"\n    privileged: true\n    x: "'
        spec = _spec(
            tmp_path,
            services=(ComposeService(name="svc", image=malicious),),
        )

        parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())

        svc = parsed["services"]["svc"]
        assert svc["image"] == malicious
        assert "privileged" not in svc

    @pytest.mark.unit
    def test_profile_service_build_fields_are_escaped_against_yaml_injection(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """Malicious ``build_context``/``dockerfile`` values cannot inject keys."""
        bad_context = 'ctx"\n    privileged: true\n    x: "'
        bad_dockerfile = 'Dockerfile"\n    volumes: ["/:/host"]\n    y: "'
        spec = _spec(
            tmp_path,
            services=(
                ComposeService(
                    name="svc",
                    build_context=bad_context,
                    dockerfile=bad_dockerfile,
                ),
            ),
        )

        parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())

        svc = parsed["services"]["svc"]
        assert svc["build"] == {"context": bad_context, "dockerfile": bad_dockerfile}
        assert "privileged" not in svc
        assert "volumes" not in svc

    @pytest.mark.unit
    def test_named_volume_key_is_escaped_against_yaml_injection(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """A malicious named-volume source cannot inject top-level compose keys.

        A named volume source (no ``/`` and no leading ``.``) is collected by
        ``_named_volumes_for`` and emitted as a top-level ``volumes:`` key — and
        into that volume's ``name:`` — by the template. ``ProfileService.volumes``
        bounds neither character set nor name pattern, so a profile from a project
        repo could embed a newline plus an outdented key. The template must
        JSON-escape both the key and the ``name`` value so the source renders as a
        single scalar instead of injecting sibling top-level entries.
        """
        malicious = 'evil"\n  injected_top_level:\n    name: "x'
        spec = _spec(
            tmp_path,
            services=(
                ComposeService(
                    name="svc",
                    image="ok:latest",
                    volumes=((malicious, "/data"),),
                ),
            ),
        )

        parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())

        assert set(parsed.keys()) == {"services", "volumes", "networks"}
        assert "injected_top_level" not in parsed["volumes"]
        assert parsed["volumes"][malicious]["name"] == f"awf-ws_test123-{malicious}"

    @pytest.mark.unit
    def test_profile_service_env_file_is_escaped_against_yaml_injection(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """A malicious ``env_file`` path cannot inject sibling compose keys.

        ``ProfileService.env_file`` only bounds length, and ``_resolve_repo_path``
        does not reject quotes/newlines, so the template must JSON-escape it (like
        ``image``/``build_context``) to keep the value a single scalar.
        """
        malicious = '.env"\n    privileged: true\n    x: "'
        spec = _spec(
            tmp_path,
            services=(ComposeService(name="svc", image="ok:latest", env_file=malicious),),
        )

        parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())

        svc = parsed["services"]["svc"]
        assert svc["env_file"] == [malicious]
        assert "privileged" not in svc

    @pytest.mark.unit
    def test_profile_service_environment_key_is_escaped_against_yaml_injection(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """A malicious environment *key* cannot inject sibling compose keys.

        ``ProfileService.environment`` is just ``dict[str, str]`` with no key
        pattern, so a project-local profile could supply a key containing YAML
        syntax (e.g. a newline plus an outdented ``privileged:``). The template
        must JSON-escape the key (like the value) so it renders as one scalar.
        """
        malicious_key = 'FOO"\n    privileged: true\n    x: "'
        spec = _spec(
            tmp_path,
            services=(
                ComposeService(
                    name="svc",
                    image="ok:latest",
                    environment=((malicious_key, "bar"),),
                ),
            ),
        )

        parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())

        svc = parsed["services"]["svc"]
        assert svc["environment"] == {malicious_key: "bar"}
        assert "privileged" not in svc

    @pytest.mark.unit
    def test_agent_environment_key_is_escaped_against_yaml_injection(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """A malicious agent environment *key* cannot inject sibling compose keys.

        ``ProfileRuntime.environment`` is just ``dict[str, str]`` with no key
        pattern, and ``profile_agent_environment()`` passes those keys straight
        into ``agent_environment``. A repository profile could supply a key
        containing YAML syntax (e.g. a newline plus an outdented ``privileged:``).
        The template must JSON-escape the agent key (like the value) so it renders
        as one scalar instead of injecting service-level settings.
        """
        malicious_key = 'FOO"\n    privileged: true\n    x: "'
        spec = _spec(
            tmp_path,
            agent_environment=((malicious_key, "bar"),),
        )

        parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())

        agent = parsed["services"]["agent"]
        assert agent["environment"][malicious_key] == "bar"
        assert "privileged" not in agent

    @pytest.mark.unit
    def test_profile_service_volume_entry_is_escaped_against_yaml_injection(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """Malicious volume source/target strings cannot inject sibling keys.

        ``ProfileService.volumes`` yields plain ``(source, target)`` strings, and
        ``_resolve_volume_source`` only rewrites repo-relative sources — quotes or
        newlines in a named-volume source or target survive. The template must
        JSON-escape the whole ``source:target`` entry (like ``image``/``command``)
        so it renders as one scalar instead of breaking out to inject keys such as
        ``privileged``.
        """
        bad_source = '/host/data"\n    privileged: true\n    x: "'
        bad_target = '/data"\n    y: "'
        spec = _spec(
            tmp_path,
            services=(
                ComposeService(
                    name="svc",
                    image="ok:latest",
                    volumes=((bad_source, bad_target),),
                ),
            ),
        )

        parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())

        svc = parsed["services"]["svc"]
        assert svc["volumes"] == [f"{bad_source}:{bad_target}"]
        assert "privileged" not in svc

    @pytest.mark.unit
    def test_companion_with_prebuilt_image_renders_image_not_build(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """A companion with a prebuilt image renders image: and suppresses build:."""
        spec = _spec(
            tmp_path,
            companions=(
                CompanionService(
                    name="backend",
                    build_context="/host/backend",
                    image="awf-companion-backend:abc123def456",
                ),
            ),
        )

        parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())

        backend = parsed["services"]["backend"]
        assert backend["image"] == "awf-companion-backend:abc123def456"
        assert "build" not in backend

    @pytest.mark.unit
    def test_companion_without_image_still_renders_build(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """A companion without a prebuilt image still renders build:."""
        spec = _spec(
            tmp_path,
            companions=(CompanionService(name="backend", build_context="/host/backend"),),
        )

        parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())

        backend = parsed["services"]["backend"]
        assert backend["build"] == {"context": "/host/backend", "dockerfile": "Dockerfile"}
        assert "image" not in backend

    @pytest.mark.unit
    def test_companion_prebuilt_image_pins_pull_policy_never(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """A companion's locally pre-built tag is never pulled from a registry.

        Companion ``awf-companion-*`` tags only ever exist on the host daemon,
        so Compose must not try to pull them. ``pull_policy: never`` turns an
        absent local image into a clear local-image-missing error instead of a
        confusing (and slow) registry pull that fails with "not found".
        """
        spec = _spec(
            tmp_path,
            companions=(
                CompanionService(
                    name="backend",
                    build_context="/host/backend",
                    image="awf-companion-backend:abc123def456",
                ),
            ),
        )

        parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())

        backend = parsed["services"]["backend"]
        assert backend["image"] == "awf-companion-backend:abc123def456"
        assert backend["pull_policy"] == "never"

    @pytest.mark.unit
    def test_profile_registry_image_is_still_pullable(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """Profile services reference registry images and must stay pullable.

        ``pull_policy: never`` is scoped to locally pre-built companion images;
        pinning it on a profile-declared registry image (postgres, redis, ...)
        would wrongly block the registry pull the service depends on.
        """
        spec = _spec(
            tmp_path,
            services=(ComposeService(name="redis", image="redis:7"),),
        )

        parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())

        redis = parsed["services"]["redis"]
        assert redis["image"] == "redis:7"
        assert "pull_policy" not in redis

    @pytest.mark.unit
    def test_project_name_is_deterministic(self, manager: ComposeManager, tmp_path: Path) -> None:
        """Compose project names and resource names are deterministic."""
        # Container names embed the workspace_id so operators can ``docker ps
        # --filter name=awf-ws_test123`` to find the stack.
        spec = _spec(tmp_path)
        assert spec.project_name() == "awf_ws_test123"

        parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())
        assert parsed["services"]["agent"]["container_name"] == "awf-ws_test123-agent"
        assert parsed["volumes"] == {}
        assert parsed["networks"]["awf_net"]["name"] == "awf-ws_test123-net"

    @pytest.mark.unit
    def test_profile_service_healthcheck_renders(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """Profile service healthchecks render into compose healthcheck blocks."""
        from awf.node.compose_manager import ComposeService

        spec = _spec(
            tmp_path,
            services=(
                ComposeService(
                    name="redis",
                    image="redis:7",
                    healthcheck_cmd="redis-cli ping",
                ),
            ),
        )
        parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())
        hc = parsed["services"]["redis"]["healthcheck"]
        assert hc["test"][0] == "CMD-SHELL"
        assert "redis-cli ping" in hc["test"][1]

    @pytest.mark.unit
    def test_agent_depends_on_profile_service_healthcheck(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """The agent waits for profile services that declare healthchecks."""
        from awf.node.compose_manager import ComposeService

        parsed = yaml.safe_load(
            manager.render(
                _spec(
                    tmp_path,
                    services=(
                        ComposeService(
                            name="redis",
                            image="redis:7",
                            healthcheck_cmd="redis-cli ping",
                        ),
                    ),
                )
            ).compose_file.read_text()
        )
        depends = parsed["services"]["agent"]["depends_on"]
        assert depends == {"redis": {"condition": "service_healthy"}}

    @pytest.mark.unit
    def test_resource_limits_applied_when_set(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """Explicit CPU and memory limits render into deploy resources."""
        spec = _spec(tmp_path, cpu_limit="4", memory_limit="8g")
        parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())
        assert parsed["services"]["agent"]["deploy"]["resources"]["limits"] == {
            "cpus": "4",
            "memory": "8g",
        }

    @pytest.mark.unit
    def test_resource_limits_apply_default_pair_when_only_one_limit_is_set(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """Single resource limits are paired with the default matching limit."""
        cpu_only = yaml.safe_load(
            manager.render(_spec(tmp_path, cpu_limit="2")).compose_file.read_text()
        )
        memory_only = yaml.safe_load(
            manager.render(_spec(tmp_path, memory_limit="2g")).compose_file.read_text()
        )

        assert cpu_only["services"]["agent"]["deploy"]["resources"]["limits"] == {
            "cpus": "2",
            "memory": "8g",
        }
        assert memory_only["services"]["agent"]["deploy"]["resources"]["limits"] == {
            "cpus": "4",
            "memory": "2g",
        }

    @pytest.mark.unit
    def test_resource_limits_absent_when_unset(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """Unset resource limits omit deploy resources from the agent service."""
        parsed = yaml.safe_load(manager.render(_spec(tmp_path)).compose_file.read_text())
        assert "deploy" not in parsed["services"]["agent"]

    @pytest.mark.unit
    def test_auth_mounts_and_git_identity_propagate(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """Auth mounts and git identity values propagate into the agent service."""
        from awf.node.compose_manager import AuthMount

        spec = _spec(
            tmp_path,
            auth_mounts=(
                AuthMount(source="/home/host/.codex", target="/home/agent/.codex", mode="rw"),
                AuthMount(source="/home/host/.ssh", target="/home/agent/.ssh", mode="ro"),
            ),
            git_name="AWF Agent",
            git_email="awf@example.com",
        )
        parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())

        volumes = parsed["services"]["agent"]["volumes"]
        assert "/home/host/.codex:/home/agent/.codex:rw" in volumes
        assert "/home/host/.ssh:/home/agent/.ssh:ro" in volumes

        env = parsed["services"]["agent"]["environment"]
        assert env["GIT_AUTHOR_NAME"] == "AWF Agent"
        assert env["GIT_COMMITTER_NAME"] == "AWF Agent"
        assert env["GIT_AUTHOR_EMAIL"] == "awf@example.com"
        assert env["GIT_COMMITTER_EMAIL"] == "awf@example.com"

    @pytest.mark.unit
    def test_declared_secret_lease_mounts_render_read_only_without_secret_values(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """Secret lease mounts render read-only without exposing secret values."""
        from awf.node.compose_manager import AuthMount

        raw_secret = "sk-live-do-not-render"
        secret_file = tmp_path / "openai-token"
        secret_file.write_text(raw_secret, encoding="utf-8")
        spec = _spec(
            tmp_path,
            auth_mounts=(
                AuthMount(
                    source=str(secret_file),
                    target="/run/awf/secrets/openai-token",
                    mode="ro",
                ),
            ),
            agent_environment=(("OPENAI_API_KEY", "${OPENAI_API_KEY}"),),
        )

        rendered = manager.render(spec).compose_file.read_text()
        parsed = yaml.safe_load(rendered)

        volumes = parsed["services"]["agent"]["volumes"]
        assert f"{secret_file}:/run/awf/secrets/openai-token:ro" in volumes
        assert parsed["services"]["agent"]["environment"]["OPENAI_API_KEY"] == ("${OPENAI_API_KEY}")
        assert raw_secret not in rendered

    @pytest.mark.unit
    def test_companion_service_renders_as_build_from_source(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """Companion services render as source builds with expected wiring."""
        from awf.node.compose_manager import CompanionService

        spec = _spec(
            tmp_path,
            companions=(
                CompanionService(
                    name="backend",
                    build_context="/host/aira-agent",
                    dockerfile="Dockerfile",
                    env_file="/host/aira-agent/.env",
                    environment=(
                        ("AIRA_DATABASE_URL", "postgresql+asyncpg://awf:pw@postgres:5432/awf"),
                    ),
                    depends_on=("postgres",),
                    healthcheck_cmd="curl -fsS http://localhost:8000/healthz || exit 1",
                    ports=((8000, 18000),),
                ),
                CompanionService(
                    name="web",
                    build_context="/host/aira-web",
                    env_file="/host/aira-web/.env.local",
                    environment=(("AGENT_SERVICE_URL", "http://backend:8000"),),
                    depends_on=("backend",),
                    healthcheck_cmd="wget -qO- http://localhost:3000 >/dev/null || exit 1",
                ),
            ),
        )
        parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())

        # Both companions present as services with build contexts.
        assert "backend" in parsed["services"]
        assert "web" in parsed["services"]

        backend = parsed["services"]["backend"]
        assert backend["build"] == {"context": "/host/aira-agent", "dockerfile": "Dockerfile"}
        assert backend["env_file"] == ["/host/aira-agent/.env"]
        assert backend["environment"] == {
            "AIRA_DATABASE_URL": "postgresql+asyncpg://awf:pw@postgres:5432/awf",
        }
        assert backend["healthcheck"]["test"][0] == "CMD-SHELL"
        assert backend["ports"] == ["18000:8000"]

        web = parsed["services"]["web"]
        assert web["environment"] == {"AGENT_SERVICE_URL": "http://backend:8000"}
        assert web["depends_on"] == {"backend": {"condition": "service_healthy"}}

        # Agent waits for healthchecked companions.
        agent_deps = parsed["services"]["agent"]["depends_on"]
        assert set(agent_deps.keys()) == {"backend", "web"}
        assert all(v == {"condition": "service_healthy"} for v in agent_deps.values())

    @pytest.mark.unit
    def test_companion_without_healthcheck_is_not_waited_on_by_agent(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """Companions without healthchecks do not gate agent startup."""
        from awf.node.compose_manager import CompanionService

        spec = _spec(
            tmp_path,
            companions=(
                CompanionService(
                    name="fire_and_forget",
                    build_context="/host/x",
                    # no healthcheck_cmd
                ),
            ),
        )
        parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())
        # fire_and_forget still exists as a service, but agent doesn't wait on it.
        assert "fire_and_forget" in parsed["services"]
        assert "depends_on" not in parsed["services"]["agent"]

    @pytest.mark.unit
    def test_named_volume_detection_ignores_bind_sources(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """Named volume detection ignores bind mounts and relative sources."""
        spec = _spec(
            tmp_path,
            services=(
                ComposeService(
                    name="cache",
                    image="busybox",
                    volumes=(
                        ("cache_data", "/cache"),
                        (str(tmp_path / "host-cache"), "/host-cache"),
                        ("./relative-cache", "/relative-cache"),
                        (".hidden-cache", "/hidden-cache"),
                    ),
                ),
            ),
        )

        parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())

        assert parsed["volumes"] == {"cache_data": {"name": "awf-ws_test123-cache_data"}}

    @pytest.mark.unit
    def test_named_volume_detection_skips_malformed_volume_entries(
        self, manager: ComposeManager
    ) -> None:
        """Named volume detection skips malformed volume entries."""
        names = manager._named_volumes_for(  # noqa: SLF001 - direct helper contract test
            [
                {"name": "not-a-list", "volumes": "cache_data:/cache"},
                {"name": "bad-items", "volumes": ["cache_data:/cache", ("too", "long", "x")]},
                {"name": "good", "volumes": [("cache_data", "/cache")]},
            ]
        )

        assert names == ["cache_data"]

    @pytest.mark.unit
    def test_dind_profile_adds_docker_daemon(self, manager: ComposeManager, tmp_path: Path) -> None:
        """DinD mode injects the managed Docker daemon service."""
        profile = docker_compose_profile()
        spec = _spec(
            tmp_path,
            docker_mode=profile.docker.mode.value,
            agent_environment=tuple(profile.runtime.environment.items()),
        )
        parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())
        assert parsed["services"]["docker"]["image"] == "docker:27-dind"
        assert parsed["services"]["docker"]["privileged"] is True
        assert parsed["services"]["docker"]["environment"]["DOCKER_TLS_CERTDIR"] == ""
        assert parsed["services"]["docker"]["healthcheck"]["test"] == [
            "CMD-SHELL",
            "docker info >/dev/null 2>&1",
        ]
        assert parsed["services"]["agent"]["environment"]["DOCKER_HOST"] == "tcp://docker:2375"
        assert parsed["services"]["agent"]["depends_on"] == {
            "docker": {"condition": "service_healthy"}
        }
        assert parsed["volumes"]["dind_data"]["name"] == "awf-ws_test123-dind_data"

    @pytest.mark.unit
    def test_dind_daemon_uses_configured_dind_image(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """DinD mode uses the configured daemon image."""
        spec = _spec(
            tmp_path,
            docker_mode="dind",
            dind_image="ghcr.io/example/dind:buildx",
        )
        parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())
        assert parsed["services"]["docker"]["image"] == "ghcr.io/example/dind:buildx"

    @pytest.mark.unit
    def test_dind_mode_sets_default_agent_docker_host(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """DinD mode supplies the default agent Docker host."""
        parsed = yaml.safe_load(
            manager.render(_spec(tmp_path, docker_mode="dind")).compose_file.read_text()
        )

        assert parsed["services"]["agent"]["environment"]["DOCKER_HOST"] == "tcp://docker:2375"

    @pytest.mark.unit
    def test_explicit_agent_docker_host_is_preserved(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """Explicit agent Docker host values override the DinD default."""
        parsed = yaml.safe_load(
            manager.render(
                _spec(
                    tmp_path,
                    docker_mode="dind",
                    agent_environment=(("DOCKER_HOST", "tcp://custom-docker:2375"),),
                )
            ).compose_file.read_text()
        )

        assert (
            parsed["services"]["agent"]["environment"]["DOCKER_HOST"] == "tcp://custom-docker:2375"
        )
