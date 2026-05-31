"""ComposeManager unit tests — template rendering only.

Docker-daemon-dependent tests live under ``tests/integration/`` and are
skipped when a daemon isn't available. These unit tests verify the rendered
compose YAML is syntactically valid and contains all the expected wiring.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import structlog
import yaml

from awf.node import compose_manager as compose_module
from awf.node.compose_manager import (
    CompanionService,
    ComposeManager,
    ComposeOperationError,
    ComposeProjectPaths,
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


class _FakeProcess:
    def __init__(
        self,
        *,
        returncode: int,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


class _HangingProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.kill_called = False

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.Event().wait()
        return b"", b""

    def kill(self) -> None:
        self.kill_called = True
        self.returncode = -9

    async def wait(self) -> int | None:
        return self.returncode


@pytest.mark.unit
def test_compose_project_paths_secret_metadata_cannot_be_mutated() -> None:
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

        assert parsed["services"]["agent"]["extra_hosts"] == ["host.docker.internal:host-gateway"]
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
        assert parsed["services"]["agent"]["extra_hosts"] == ["host.docker.internal:host-gateway"]

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
    def test_dind_companion_environment_secret_placeholder_is_rendered_without_raw_value(
        self,
        manager: ComposeManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
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
    def test_declared_secret_lease_mounts_render_read_only_without_secret_values(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
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

        assert parsed["volumes"] == {"cache_data": {"name": "awf-ws_test123-cache_data"}}

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
    def test_dind_daemon_uses_configured_dind_image(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
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
            parsed["services"]["agent"]["environment"]["DOCKER_HOST"] == "tcp://custom-docker:2375"
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

    @pytest.mark.unit
    async def test_remove_project_by_label_removes_containers_networks_and_volumes(
        self,
        tmp_path: Path,
    ) -> None:
        class _RecordingComposeManager(ComposeManager):
            def __init__(self) -> None:
                super().__init__(work_dir=tmp_path / "work", template_path=_TEMPLATE)
                self.calls: list[tuple[str, ...]] = []

            async def _docker_capture(self, args: list[str], *, operation: str) -> str:
                self.calls.append((operation, *args))
                if operation == "ps":
                    return "container-a\ncontainer-b\n"
                if operation == "network ls":
                    return "network-a\nnetwork-b\n"
                if operation == "volume ls":
                    return "volume-a\nvolume-b\n"
                return ""

        manager = _RecordingComposeManager()

        with structlog.testing.capture_logs() as captured:
            await manager.remove_project_by_label(
                project_name="awf_ws_lost",
                workspace_id="ws_lost",
                remove_volumes=True,
            )

        label_filter = "label=com.docker.compose.project=awf_ws_lost"
        assert manager.calls == [
            ("ps", "ps", "-aq", "--filter", label_filter),
            ("rm", "rm", "-f", "container-a", "container-b"),
            ("network ls", "network", "ls", "-q", "--filter", label_filter),
            ("network rm", "network", "rm", "network-a"),
            ("network rm", "network", "rm", "network-b"),
            ("volume ls", "volume", "ls", "-q", "--filter", label_filter),
            ("volume rm", "volume", "rm", "-f", "volume-a", "volume-b"),
        ]
        event = next(item for item in captured if item["event"] == "compose.project_label_removed")
        assert event["containers"] == 2
        assert event["networks"] == 2
        assert event["volumes"] == 2

    @pytest.mark.unit
    async def test_remove_project_by_label_can_keep_volumes(
        self,
        tmp_path: Path,
    ) -> None:
        class _RecordingComposeManager(ComposeManager):
            def __init__(self) -> None:
                super().__init__(work_dir=tmp_path / "work", template_path=_TEMPLATE)
                self.calls: list[tuple[str, ...]] = []

            async def _docker_capture(self, args: list[str], *, operation: str) -> str:
                self.calls.append((operation, *args))
                if operation == "ps":
                    return ""
                if operation == "network ls":
                    return "network-a\n"
                if operation == "volume ls":
                    raise AssertionError("volume listing should be skipped")
                return ""

        manager = _RecordingComposeManager()

        with structlog.testing.capture_logs() as captured:
            await manager.remove_project_by_label(
                project_name="awf_ws_keep_volumes",
                workspace_id="ws_keep_volumes",
                remove_volumes=False,
            )

        label_filter = "label=com.docker.compose.project=awf_ws_keep_volumes"
        assert manager.calls == [
            ("ps", "ps", "-aq", "--filter", label_filter),
            ("network ls", "network", "ls", "-q", "--filter", label_filter),
            ("network rm", "network", "rm", "network-a"),
        ]
        event = next(item for item in captured if item["event"] == "compose.project_label_removed")
        assert event["volumes"] == 0

    @pytest.mark.unit
    async def test_compose_command_reports_missing_docker_binary(
        self,
        manager: ComposeManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _raise_missing(*_args: object, **_kwargs: object) -> _FakeProcess:
            raise FileNotFoundError("docker")

        monkeypatch.setattr(
            compose_module.asyncio,
            "create_subprocess_exec",
            _raise_missing,
        )

        with pytest.raises(ComposeOperationError) as exc:
            await manager._compose(  # noqa: SLF001
                "awf_ws_missing_docker",
                tmp_path / "compose.yml",
                ["up", "-d"],
                operation="up",
            )

        assert exc.value.returncode == 127
        assert exc.value.reason_code == "DOCKER_UNAVAILABLE"
        assert "docker" in exc.value.stderr

    @pytest.mark.unit
    async def test_compose_command_uses_environment_project_and_file_contract(
        self,
        manager: ComposeManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        async def _spawn(*args: object, **kwargs: object) -> _FakeProcess:
            calls.append((args, kwargs))
            return _FakeProcess(returncode=0)

        compose_file = tmp_path / "compose.yml"
        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        await manager._compose(  # noqa: SLF001
            "awf_ws_long_flags",
            compose_file,
            ["up", "-d"],
            operation="up",
        )

        assert calls
        args, kwargs = calls[0]
        assert args == ("docker", "compose", "up", "-d")
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert env["COMPOSE_PROJECT_NAME"] == "awf_ws_long_flags"
        assert env["COMPOSE_FILE"] == str(compose_file)

    @pytest.mark.unit
    async def test_ensure_project_up_without_wait_omits_wait_flags(
        self,
        manager: ComposeManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[tuple[object, ...]] = []

        async def _spawn(*args: object, **_kwargs: object) -> _FakeProcess:
            calls.append(args)
            return _FakeProcess(returncode=0)

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        await manager.ensure_project_up(
            project_name="awf_ws_resume",
            compose_file=tmp_path / "compose.yml",
            workspace_id="ws_resume",
            wait=False,
        )

        assert calls
        assert calls[0] == ("docker", "compose", "up", "-d", "--remove-orphans")
        assert "--wait" not in calls[0]

    @pytest.mark.unit
    async def test_compose_command_times_out_and_kills_hung_process(
        self,
        manager: ComposeManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        process = _HangingProcess()

        async def _spawn(*_args: object, **_kwargs: object) -> _HangingProcess:
            return process

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        with pytest.raises(ComposeOperationError) as exc:
            await manager._compose(  # noqa: SLF001
                "awf_ws_hung_compose",
                tmp_path / "compose.yml",
                ["up", "-d", "--wait"],
                operation="up",
                capture_timeout_seconds=0.01,
            )

        assert process.kill_called is True
        assert exc.value.returncode == 124
        assert exc.value.reason_code == "DOCKER_COMMAND_TIMEOUT"
        assert "docker compose up exceeded 0.01s timeout" in exc.value.stderr

    @pytest.mark.unit
    async def test_docker_capture_returns_stdout_on_success(
        self,
        manager: ComposeManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[tuple[object, ...]] = []

        async def _spawn(*args: object, **_kwargs: object) -> _FakeProcess:
            calls.append(args)
            return _FakeProcess(returncode=0, stdout=b"container-a\ncontainer-b\n")

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        stdout = await manager._docker_capture(["ps", "-aq"], operation="ps")  # noqa: SLF001

        assert stdout == "container-a\ncontainer-b\n"
        assert calls == [("docker", "ps", "-aq")]

    @pytest.mark.unit
    async def test_docker_capture_classifies_daemon_connectivity_errors(
        self,
        manager: ComposeManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _spawn(*_args: object, **_kwargs: object) -> _FakeProcess:
            return _FakeProcess(
                returncode=1,
                stderr=b"error during connect: docker endpoint unavailable",
            )

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        with pytest.raises(ComposeOperationError) as exc:
            await manager._docker_capture(["network", "ls"], operation="network ls")  # noqa: SLF001

        assert exc.value.returncode == 1
        assert exc.value.reason_code == "DOCKER_UNAVAILABLE"
        assert "docker endpoint unavailable" in exc.value.stderr

    @pytest.mark.unit
    async def test_docker_capture_classifies_command_failures(
        self,
        manager: ComposeManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _spawn(*_args: object, **_kwargs: object) -> _FakeProcess:
            return _FakeProcess(returncode=2, stdout=b"usage\n", stderr=b"bad flag\n")

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        with pytest.raises(ComposeOperationError) as exc:
            await manager._docker_capture(["volume", "rm"], operation="volume rm")  # noqa: SLF001

        assert exc.value.returncode == 2
        assert exc.value.reason_code == "COMPOSE_COMMAND_FAILED"
        assert exc.value.stdout == "usage\n"
        assert exc.value.stderr == "bad flag\n"

    @pytest.mark.unit
    async def test_docker_capture_reports_missing_docker_binary(
        self,
        manager: ComposeManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _raise_missing(*_args: object, **_kwargs: object) -> _FakeProcess:
            raise FileNotFoundError("docker")

        monkeypatch.setattr(
            compose_module.asyncio,
            "create_subprocess_exec",
            _raise_missing,
        )

        with pytest.raises(ComposeOperationError) as exc:
            await manager._docker_capture(["ps"], operation="ps")  # noqa: SLF001

        assert exc.value.returncode == 127
        assert exc.value.reason_code == "DOCKER_UNAVAILABLE"

    @pytest.mark.unit
    async def test_docker_capture_translates_non_filenotfound_oserror(
        self,
        manager: ComposeManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``PermissionError`` (docker binary present but not executable) is an
        # ``OSError`` subclass that is *not* ``FileNotFoundError``; it must still be
        # translated to a structured ``DOCKER_UNAVAILABLE`` error so best-effort
        # callers like ``capture_companion_diagnostics`` never leak a raw ``OSError``.
        async def _raise_permission(*_args: object, **_kwargs: object) -> _FakeProcess:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(
            compose_module.asyncio,
            "create_subprocess_exec",
            _raise_permission,
        )

        with pytest.raises(ComposeOperationError) as exc:
            await manager._docker_capture(["ps"], operation="ps")  # noqa: SLF001

        assert exc.value.returncode == 127
        assert exc.value.reason_code == "DOCKER_UNAVAILABLE"
        assert "Permission denied" in exc.value.stderr

    @pytest.mark.unit
    async def test_docker_capture_times_out_and_kills_hung_process(
        self,
        manager: ComposeManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        process = _HangingProcess()

        async def _spawn(*_args: object, **_kwargs: object) -> _HangingProcess:
            return process

        monkeypatch.setattr(compose_module, "DOCKER_CAPTURE_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        with pytest.raises(ComposeOperationError) as exc:
            await manager._docker_capture(["ps"], operation="ps")  # noqa: SLF001

        assert process.kill_called is True
        assert exc.value.returncode == 124
        assert exc.value.reason_code == "DOCKER_COMMAND_TIMEOUT"
        assert "exceeded" in exc.value.stderr


class TestCompanionImageCommands:
    """Tests for the ComposeManager companion image Docker commands."""

    @pytest.mark.unit
    async def test_companion_image_exists_true_on_zero_exit(
        self, manager: ComposeManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """companion_image_exists returns True on a zero-exit inspect."""
        calls: list[tuple[object, ...]] = []

        async def _spawn(*args: object, **_kwargs: object) -> _FakeProcess:
            calls.append(args)
            return _FakeProcess(returncode=0, stdout=b"sha256:abc\n")

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        assert await manager.companion_image_exists("awf-companion-backend:abc") is True
        assert calls[0] == ("docker", "image", "inspect", "awf-companion-backend:abc")

    @pytest.mark.unit
    async def test_companion_image_exists_false_when_inspect_fails(
        self, manager: ComposeManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """companion_image_exists returns False when the inspect fails."""

        async def _spawn(*_args: object, **_kwargs: object) -> _FakeProcess:
            return _FakeProcess(returncode=1, stderr=b"No such image")

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        assert await manager.companion_image_exists("missing:tag") is False

    @pytest.mark.unit
    async def test_build_companion_image_passes_tag_dockerfile_and_labels(
        self, manager: ComposeManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """build_companion_image passes the tag, Dockerfile, and managed labels."""
        calls: list[tuple[object, ...]] = []

        async def _spawn(*args: object, **_kwargs: object) -> _FakeProcess:
            calls.append(args)
            return _FakeProcess(returncode=0)

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        await manager.build_companion_image(
            tag="awf-companion-backend:abc",
            build_context="/host/backend",
            dockerfile="Dockerfile",
            labels={"awf.managed-companion": "true", "awf.companion.name": "backend"},
        )

        assert calls[0] == (
            "docker",
            "build",
            "-t",
            "awf-companion-backend:abc",
            "-f",
            "/host/backend/Dockerfile",
            "--label",
            "awf.managed-companion=true",
            "--label",
            "awf.companion.name=backend",
            "/host/backend",
        )

    @pytest.mark.unit
    async def test_build_companion_image_without_labels_omits_label_flags(
        self, manager: ComposeManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """build_companion_image omits the label flags when no labels are given."""
        calls: list[tuple[object, ...]] = []

        async def _spawn(*args: object, **_kwargs: object) -> _FakeProcess:
            calls.append(args)
            return _FakeProcess(returncode=0)

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        await manager.build_companion_image(
            tag="awf-companion-backend:abc",
            build_context="/host/backend",
            dockerfile="Dockerfile",
        )

        assert calls[0] == (
            "docker",
            "build",
            "-t",
            "awf-companion-backend:abc",
            "-f",
            "/host/backend/Dockerfile",
            "/host/backend",
        )
        assert "--label" not in calls[0]

    @pytest.mark.unit
    async def test_build_companion_image_anchors_dockerfile_to_build_context(
        self, manager: ComposeManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression for PRRT_kwDOSJAM6s6F5073: ``docker build`` resolves a ``-f``
        # path relative to the process working directory (the AWF service cwd),
        # not the build context. A context-relative ``dockerfile`` must be
        # anchored to the absolute build context so the pre-build does not look
        # for the Dockerfile under the service cwd, fail, and fall back to an
        # inline compose build.
        """build_companion_image anchors the Dockerfile path to the build context."""
        calls: list[tuple[object, ...]] = []

        async def _spawn(*args: object, **_kwargs: object) -> _FakeProcess:
            calls.append(args)
            return _FakeProcess(returncode=0)

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        await manager.build_companion_image(
            tag="awf-companion-backend:abc",
            build_context="/host/aira-agent",
            dockerfile="docker/backend.Dockerfile",
        )

        assert calls[0] == (
            "docker",
            "build",
            "-t",
            "awf-companion-backend:abc",
            "-f",
            "/host/aira-agent/docker/backend.Dockerfile",
            "/host/aira-agent",
        )

    @pytest.mark.unit
    async def test_build_companion_image_raises_on_failure(
        self, manager: ComposeManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """build_companion_image raises when the docker build fails."""

        async def _spawn(*_args: object, **_kwargs: object) -> _FakeProcess:
            return _FakeProcess(returncode=1, stderr=b"build failed")

        monkeypatch.setattr(compose_module.asyncio, "create_subprocess_exec", _spawn)

        with pytest.raises(ComposeOperationError):
            await manager.build_companion_image(
                tag="awf-companion-backend:abc",
                build_context="/host/backend",
                dockerfile="Dockerfile",
            )
