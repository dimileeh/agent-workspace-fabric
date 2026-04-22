"""ComposeManager unit tests — template rendering only.

Docker-daemon-dependent tests live under ``tests/integration/`` and are
skipped when a daemon isn't available. These unit tests verify the rendered
compose YAML is syntactically valid and contains all the expected wiring.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf.node.compose_manager import ComposeManager, WorkspaceComposeSpec

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
        assert set(parsed["services"].keys()) == {"postgres", "agent"}

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
    def test_postgres_password_propagates_to_database_url(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        spec = _spec(tmp_path, postgres_password="my-secret-1234")
        paths = manager.render(spec)

        parsed = yaml.safe_load(paths.compose_file.read_text())
        agent_env = parsed["services"]["agent"]["environment"]
        pg_env = parsed["services"]["postgres"]["environment"]

        assert pg_env["POSTGRES_PASSWORD"] == "my-secret-1234"
        assert "my-secret-1234" in agent_env["DATABASE_URL"]
        assert agent_env["DATABASE_URL"].startswith("postgresql+psycopg://awf:")

    @pytest.mark.unit
    def test_unset_password_generates_one_each_render(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        spec = _spec(tmp_path, postgres_password=None)
        first = yaml.safe_load(manager.render(spec).compose_file.read_text())
        second = yaml.safe_load(manager.render(spec).compose_file.read_text())

        # Two distinct invocations = two distinct passwords (tokens are ~24 chars).
        pw1 = first["services"]["postgres"]["environment"]["POSTGRES_PASSWORD"]
        pw2 = second["services"]["postgres"]["environment"]["POSTGRES_PASSWORD"]
        assert pw1 != pw2
        assert len(pw1) >= 20

    @pytest.mark.unit
    def test_project_name_is_deterministic(self, manager: ComposeManager, tmp_path: Path) -> None:
        # Container names embed the workspace_id so operators can ``docker ps
        # --filter name=awf-ws_test123`` to find the stack.
        spec = _spec(tmp_path)
        assert spec.project_name() == "awf_ws_test123"

        parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())
        assert parsed["services"]["postgres"]["container_name"] == "awf-ws_test123-postgres"
        assert parsed["services"]["agent"]["container_name"] == "awf-ws_test123-agent"
        assert parsed["volumes"]["postgres_data"]["name"] == "awf-ws_test123-pgdata"
        assert parsed["networks"]["awf_net"]["name"] == "awf-ws_test123-net"

    @pytest.mark.unit
    def test_healthcheck_uses_pg_isready(self, manager: ComposeManager, tmp_path: Path) -> None:
        parsed = yaml.safe_load(manager.render(_spec(tmp_path)).compose_file.read_text())
        hc = parsed["services"]["postgres"]["healthcheck"]
        assert hc["test"][0] == "CMD-SHELL"
        assert "pg_isready" in hc["test"][1]

    @pytest.mark.unit
    def test_agent_depends_on_postgres_healthy(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        parsed = yaml.safe_load(manager.render(_spec(tmp_path)).compose_file.read_text())
        depends = parsed["services"]["agent"]["depends_on"]
        assert depends == {"postgres": {"condition": "service_healthy"}}

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
