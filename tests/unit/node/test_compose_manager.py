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
    ComposeService,
    WorkspaceComposeSpec,
)
from awf.profiles.registry import docker_compose_profile

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


@pytest.fixture
def manager(tmp_path: Path) -> ComposeManager:
    return ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)


def _spec(tmp_path: Path, **overrides: object) -> WorkspaceComposeSpec:
    base = {
        "workspace_id": "ws_test123",
        "worktree_host_path": tmp_path / "worktree",
        "postgres_password": "deterministic-for-test",
    }
    base.update(overrides)
    return WorkspaceComposeSpec(**base)  # type: ignore[arg-type]


class TestRender:
    @pytest.mark.unit
    def test_renders_valid_yaml(self, manager: ComposeManager, tmp_path: Path) -> None:
        paths = manager.render(_spec(tmp_path))
        assert paths.compose_file.exists()

        parsed = yaml.safe_load(paths.compose_file.read_text())
        assert set(parsed.keys()) == {"services", "volumes", "networks"}
        assert set(parsed["services"].keys()) == {"agent"}

    @pytest.mark.unit
    def test_mounts_worktree_into_agent_at_workspace(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
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
        rendered = manager.render(_spec(tmp_path)).compose_file.read_text()
        parsed = yaml.safe_load(rendered)

        assert parsed["services"]["agent"]["extra_hosts"] == [
            "host.docker.internal:host-gateway"
        ]
        assert "\n    \n    extra_hosts:" not in rendered

    @pytest.mark.unit
    def test_open_egress_policy_keeps_public_network_and_host_gateway(
        self,
        manager: ComposeManager,
        tmp_path: Path,
    ) -> None:
        parsed = yaml.safe_load(
            manager.render(
                _spec(tmp_path, network_internal=False, host_gateway_enabled=True)
            ).compose_file.read_text()
        )

        assert "internal" not in parsed["networks"]["awf_net"]
        assert parsed["services"]["agent"]["extra_hosts"] == [
            "host.docker.internal:host-gateway"
        ]

    @pytest.mark.unit
    def test_offline_egress_policy_renders_internal_network_without_host_gateway(
        self,
        manager: ComposeManager,
        tmp_path: Path,
    ) -> None:
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
    def test_project_name_is_deterministic(self, manager: ComposeManager, tmp_path: Path) -> None:
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
        parsed = yaml.safe_load(manager.render(_spec(tmp_path)).compose_file.read_text())
        assert "deploy" not in parsed["services"]["agent"]

    @pytest.mark.unit
    def test_auth_mounts_and_git_identity_propagate(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
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
    def test_companion_service_renders_as_build_from_source(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
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

        assert parsed["volumes"] == {
            "cache_data": {"name": "awf-ws_test123-cache_data"}
        }

    @pytest.mark.unit
    def test_named_volume_detection_skips_malformed_volume_entries(
        self, manager: ComposeManager
    ) -> None:
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
    def test_dind_mode_sets_default_agent_docker_host(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        parsed = yaml.safe_load(
            manager.render(_spec(tmp_path, docker_mode="dind")).compose_file.read_text()
        )

        assert parsed["services"]["agent"]["environment"]["DOCKER_HOST"] == "tcp://docker:2375"

    @pytest.mark.unit
    def test_explicit_agent_docker_host_is_preserved(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
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
            parsed["services"]["agent"]["environment"]["DOCKER_HOST"]
            == "tcp://custom-docker:2375"
        )

    @pytest.mark.unit
    def test_strict_undefined_catches_missing_vars(self) -> None:
        # Guard: if the template starts referencing a new variable without the
        # WorkspaceComposeSpec supplying it, rendering must fail loudly rather
        # than silently emitting empty YAML values.
        from jinja2 import Environment, StrictUndefined
        from jinja2.exceptions import UndefinedError

        env = Environment(undefined=StrictUndefined, autoescape=False)
        tmpl = env.from_string("name: {{ only_in_template }}")
        with pytest.raises(UndefinedError):
            tmpl.render()

    @pytest.mark.unit
    async def test_down_project_is_noop_when_compose_file_is_missing(
        self,
        tmp_path: Path,
    ) -> None:
        class _RecordingComposeManager(ComposeManager):
            def __init__(self) -> None:
                super().__init__(work_dir=tmp_path / "work", template_path=_TEMPLATE)
                self.calls: list[tuple[str, Path, list[str], str]] = []

            async def _compose(
                self,
                project_name: str,
                compose_file: Path,
                args: list[str],
                *,
                operation: str,
            ) -> None:
                self.calls.append((project_name, compose_file, args, operation))

        manager = _RecordingComposeManager()

        await manager.down_project(
            project_name="awf_ws_missing",
            compose_file=tmp_path / "missing-compose.yml",
            workspace_id="ws_missing",
        )

        assert manager.calls == []
