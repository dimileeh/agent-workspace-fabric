"""Static contract tests for the local AWF service Docker Compose stack."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BOOTSTRAP_ASSET_PATHS = (
    ".env.example",
    "docker/agent-runtime.Dockerfile",
    "docker/compose/local-service.yml",
    "docker/control-plane.Dockerfile",
    "pyproject.toml",
    "src/awf/__init__.py",
)


def _copy_bootstrap_assets(checkout: Path) -> None:
    """Copy real source assets into a temporary checkout shape."""
    for relative_path in _BOOTSTRAP_ASSET_PATHS:
        source = _REPO_ROOT / relative_path
        target = checkout / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _compose_template_token_value(template: str, inner: str, env: dict[str, str]) -> str:
    if not inner:
        raise ValueError("Compose template variable name must not be empty")

    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)(?:(:-|:\?|:\+|-|\?|\+)(.*))?", inner)
    if match is None:
        raise ValueError(f"Unsupported Compose template: {template}")
    key, operator, operand = match.groups(default="")
    resolved = env.get(key)
    if operator == "":
        return env.get(key, "")
    if operator == ":-":
        return operand if resolved in (None, "") else resolved
    if operator == "-":
        return operand if resolved is None else resolved
    if operator == ":?":
        if resolved in (None, ""):
            raise ValueError(operand or f"{key} is required")
        return resolved
    if operator == "?":
        if resolved is None:
            raise ValueError(operand or f"{key} is required")
        return resolved
    if operator == ":+":
        return operand if resolved not in (None, "") else ""
    return operand if resolved is not None else ""


def _compose_template_value(value: str, env: dict[str, str]) -> str:
    rendered: list[str] = []
    index = 0
    while index < len(value):
        if value.startswith("$$", index):
            rendered.append("$")
            index += 2
            continue
        if value.startswith("${", index):
            end = value.find("}", index + 2)
            if end == -1:
                rendered.append(value[index])
                index += 1
                continue
            template = value[index : end + 1]
            rendered.append(
                _compose_template_token_value(template, template[2:-1], env),
            )
            index = end + 1
            continue
        rendered.append(value[index])
        index += 1
    return "".join(rendered)


def _compose_short_port_mapping_fields(mapping: str) -> list[str]:
    fields: list[str] = []
    current: list[str] = []
    in_template = False
    in_brackets = False
    index = 0

    while index < len(mapping):
        if not in_template and mapping.startswith("${", index):
            in_template = True
            current.append("${")
            index += 2
            continue

        char = mapping[index]
        if in_template:
            current.append(char)
            if char == "}":
                in_template = False
        elif char == "[":
            in_brackets = True
            current.append(char)
        elif char == "]":
            in_brackets = False
            current.append(char)
        elif char == ":" and not in_brackets:
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1

    fields.append("".join(current))
    return fields


def _compose_published_host_port(mapping: str) -> str:
    fields = _compose_short_port_mapping_fields(mapping)
    if len(fields) < 2:
        raise ValueError(f"Compose port mapping has no published host port: {mapping}")
    return fields[-2]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mapping", "expected"),
    [
        ("${AWF_API_HOST_PORT:-8000}:8000", "${AWF_API_HOST_PORT:-8000}"),
        ("127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432", "${AWF_POSTGRES_HOST_PORT:-5433}"),
        ("0.0.0.0:${AWF_POSTGRES_HOST_PORT:-5433}:5432", "${AWF_POSTGRES_HOST_PORT:-5433}"),
        (
            "${AWF_BIND_IP:-127.0.0.1}:${AWF_POSTGRES_HOST_PORT:-5433}:5432",
            "${AWF_POSTGRES_HOST_PORT:-5433}",
        ),
        ("[::1]:${AWF_POSTGRES_HOST_PORT:-5433}:5432", "${AWF_POSTGRES_HOST_PORT:-5433}"),
    ],
)
def test_compose_published_host_port_extracts_port_template(mapping: str, expected: str) -> None:
    assert _compose_published_host_port(mapping) == expected


@pytest.mark.integration
def test_local_service_compose_declares_control_plane_stack() -> None:
    compose_path = Path("docker/compose/local-service.yml")
    dockerfile_path = Path("docker/control-plane.Dockerfile")

    assert compose_path.exists()
    assert dockerfile_path.exists()
    data = yaml.safe_load(compose_path.read_text())
    services = data["services"]

    assert {"postgres", "migrate", "api", "worker", "ollama-bridge"}.issubset(services)

    for service_name in ("migrate", "api", "worker"):
        assert services[service_name]["image"] == "awf-control-plane:local"
        assert services[service_name]["build"] == {
            "context": "../..",
            "dockerfile": "docker/control-plane.Dockerfile",
        }

    dockerfile = dockerfile_path.read_text()
    assert "ARG DOCKER_CE_CLI_VERSION=" in dockerfile
    assert '"docker-ce-cli=${DOCKER_CE_CLI_VERSION}"' in dockerfile
    assert "ARG DOCKER_COMPOSE_PLUGIN_VERSION=" in dockerfile
    assert '"docker-compose-plugin=${DOCKER_COMPOSE_PLUGIN_VERSION}"' in dockerfile
    assert "githubcli-archive-keyring.gpg" in dockerfile
    assert "gh" in dockerfile
    assert "COPY src ./src" in dockerfile
    assert "COPY migrations ./migrations" in dockerfile
    assert (
        "COPY docker/compose/workspace.base.yml.j2 ./docker/compose/workspace.base.yml.j2"
        in dockerfile
    )
    assert "uv sync --frozen --extra dev" in dockerfile

    expected_work_dir = "${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}"
    expected_host_home = "${AWF_HOST_HOME:-${HOME}}"
    expected_ssh_auth_sock_source = (
        "${AWF_HOST_SSH_AUTH_SOCK:-${SSH_AUTH_SOCK:-/run/host-services/ssh-auth.sock}}"
    )
    expected_ssh_auth_sock_target = "/run/host-services/ssh-auth.sock"
    expected_auth_mounts = {
        f"{expected_host_home}/.config/gh:{expected_host_home}/.config/gh:ro",
        f"{expected_host_home}/.config/gcloud:{expected_host_home}/.config/gcloud:ro",
        f"{expected_host_home}/.gitconfig:{expected_host_home}/.gitconfig:ro",
        f"{expected_host_home}/.ssh:{expected_host_home}/.ssh:ro",
        f"{expected_host_home}/.codex:{expected_host_home}/.codex:ro",
        f"{expected_host_home}/.claude:{expected_host_home}/.claude:ro",
        f"{expected_host_home}/.claude.json:{expected_host_home}/.claude.json:ro",
        f"{expected_host_home}/.gemini:{expected_host_home}/.gemini:ro",
        f"{expected_host_home}/.config/opencode:{expected_host_home}/.config/opencode:ro",
        f"{expected_host_home}/.ollama:{expected_host_home}/.ollama:ro",
    }
    expected_base_mounts = {
        "/var/run/docker.sock:/var/run/docker.sock",
        f"{expected_ssh_auth_sock_source}:{expected_ssh_auth_sock_target}",
        f"{expected_work_dir}:{expected_work_dir}",
    }
    # The worker mounts the host work dir ``:rshared`` so an overlay it mounts
    # under the work dir is visible to the sibling agent container the host
    # daemon launches (see docker/compose/local-service.yml). It is the same
    # base mount set with the work-dir bind carrying the propagation flag.
    expected_worker_base_mounts = {
        "/var/run/docker.sock:/var/run/docker.sock",
        f"{expected_ssh_auth_sock_source}:{expected_ssh_auth_sock_target}",
        f"{expected_work_dir}:{expected_work_dir}:rshared",
    }
    shared_volumes = data["x-awf-service"]["volumes"]
    assert expected_base_mounts.issubset(set(shared_volumes))
    assert expected_auth_mounts.isdisjoint(set(shared_volumes))

    migrate_volumes = services["migrate"]["volumes"]
    assert expected_base_mounts.issubset(set(migrate_volumes))
    assert expected_auth_mounts.isdisjoint(set(migrate_volumes))

    for service_name in ("api", "worker"):
        volumes = services[service_name]["volumes"]
        assert "../..:/app" not in volumes
        assert f"{expected_host_home}:{expected_host_home}:ro" not in volumes
        assert services[service_name]["extra_hosts"] == ["host.docker.internal:host-gateway"]
        service_base_mounts = (
            expected_worker_base_mounts if service_name == "worker" else expected_base_mounts
        )
        assert service_base_mounts.issubset(set(volumes))
        assert expected_auth_mounts.issubset(set(volumes))
        environment = services[service_name]["environment"]
        assert environment["AWF_API_BASE_URL"] == "http://api:8000"
        assert environment["AWF_API_TOKEN"] == "${AWF_API_TOKEN:?set AWF_API_TOKEN}"
        assert environment["AWF_DATABASE_URL"] == (
            "postgresql+asyncpg://awf:${AWF_POSTGRES_PASSWORD:?set "
            "AWF_POSTGRES_PASSWORD}@postgres:5432/awf"
        )
        assert "awf_dev" not in environment["AWF_DATABASE_URL"]
        assert environment["AWF_WORK_DIR"] == expected_work_dir
        assert environment["AWF_HOST_HOME"] == expected_host_home
        assert (
            environment["GOOGLE_APPLICATION_CREDENTIALS"] == "${GOOGLE_APPLICATION_CREDENTIALS:-}"
        )
        assert environment["AWF_OPENCODE_OLLAMA_BASE_URL"] == "${AWF_OPENCODE_OLLAMA_BASE_URL:-}"
        assert environment["OLLAMA_HOST"] == "${OLLAMA_HOST:-}"
        assert environment["OLLAMA_API_KEY"] == "${OLLAMA_API_KEY:-}"
        assert environment["AWF_AGENT_IDLE_TIMEOUT_SECONDS"] == (
            "${AWF_AGENT_IDLE_TIMEOUT_SECONDS:-3600}"
        )
        assert environment["AWF_WORKSPACE_STEADY_CPU"] == "${AWF_WORKSPACE_STEADY_CPU:-3}"
        assert environment["AWF_WORKSPACE_STEADY_MEMORY_GB"] == (
            "${AWF_WORKSPACE_STEADY_MEMORY_GB:-10}"
        )
        assert environment["AWF_WORKSPACE_PEAK_CPU"] == "${AWF_WORKSPACE_PEAK_CPU:-6}"
        assert environment["AWF_WORKSPACE_PEAK_MEMORY_GB"] == (
            "${AWF_WORKSPACE_PEAK_MEMORY_GB:-16}"
        )
        assert environment["AWF_LOCAL_CAPACITY_CPU_CORES"] == ("${AWF_LOCAL_CAPACITY_CPU_CORES:-}")
        assert environment["AWF_LOCAL_CAPACITY_MEMORY_GB"] == ("${AWF_LOCAL_CAPACITY_MEMORY_GB:-}")
        assert environment["AWF_LOCAL_CAPACITY_DIND_SLOTS"] == (
            "${AWF_LOCAL_CAPACITY_DIND_SLOTS:-}"
        )
        assert environment["SSH_AUTH_SOCK"] == expected_ssh_auth_sock_target

    postgres = services["postgres"]
    assert postgres["environment"]["POSTGRES_PASSWORD"] == (
        "${AWF_POSTGRES_PASSWORD:?set AWF_POSTGRES_PASSWORD}"
    )
    assert "awf_dev" not in yaml.safe_dump(postgres["environment"])
    assert postgres["ports"] == ["127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432"]

    api = services["api"]
    assert api["ports"] == ["${AWF_API_HOST_PORT:-8000}:8000"]

    assert "awf-work" not in data.get("volumes", {})
    migrate_command = services["migrate"]["command"]
    assert "alembic upgrade head" in migrate_command
    assert "uv run" not in migrate_command

    for service_name in ("api", "worker"):
        assert "uv run" not in services[service_name]["command"]
        depends_on = services[service_name]["depends_on"]
        assert depends_on["postgres"]["condition"] == "service_healthy"
        assert depends_on["migrate"]["condition"] == "service_completed_successfully"

    bridge = services["ollama-bridge"]
    assert bridge["profiles"] == ["ollama-bridge"]
    assert bridge["image"] == "alpine/socat:1.8.0.3"
    assert bridge["network_mode"] == "host"
    assert bridge["command"] == [
        "TCP-LISTEN:${AWF_OLLAMA_BRIDGE_LISTEN_PORT:-11434},bind=${AWF_OLLAMA_BRIDGE_BIND_ADDRESS:-172.17.0.1},fork,reuseaddr",
        "TCP:${AWF_OLLAMA_BRIDGE_TARGET_HOST:-127.0.0.1}:${AWF_OLLAMA_BRIDGE_TARGET_PORT:-11434}",
    ]


@pytest.mark.integration
def test_init_env_seeding_uses_real_source_checkout_compose_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise init env target discovery and seeding against real AWF assets."""
    from awf.cli import init_ops as cli_init_ops
    from awf.service.config import local_service_environ

    checkout = tmp_path / "checkout"
    _copy_bootstrap_assets(checkout)
    root_env = checkout / ".env"
    root_env.write_text(
        "\n".join(
            [
                "AWF_API_TOKEN=operator-token",
                "AWF_POSTGRES_PASSWORD=operator-password",
                "",
                "# Operator Docker socket",
                "AWF_DOCKER_HOST=unix:///tmp/awf-review.sock",
                "AWF_ROOT_ONLY=operator-only",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    nested_project = checkout / "projects" / "demo"
    nested_project.mkdir(parents=True)
    monkeypatch.chdir(nested_project)

    compose_file, env_file, env_example = cli_init_ops._resolve_service_compose_paths()  # noqa: SLF001

    assert compose_file == checkout / "docker" / "compose" / "local-service.yml"
    assert env_file == checkout / "docker" / "compose" / ".env"
    assert env_example == checkout / ".env.example"
    assert not env_file.exists()

    action, error, overlay_keys = cli_init_ops._seed_env_file(  # noqa: SLF001
        env_file,
        env_example,
        env_overlay=cli_init_ops._init_env_overlay_source(env_file, env_example),  # noqa: SLF001
    )

    assert action == "wrote_from_example"
    assert error is None
    assert overlay_keys == ("AWF_ROOT_ONLY",)
    env_text = env_file.read_text(encoding="utf-8")
    assert "AWF_API_TOKEN=operator-token\n" in env_text
    assert "AWF_POSTGRES_PASSWORD=operator-password\n" in env_text
    assert "AWF_WORKSPACE_STEADY_CPU=3\n" in env_text
    assert "# Operator Docker socket\nAWF_DOCKER_HOST=unix:///tmp/awf-review.sock\n" in env_text
    assert "AWF_ROOT_ONLY=operator-only\n" in env_text

    active_env_file, compose_env_file = cli_init_ops._resolve_service_env_files(env_file)  # noqa: SLF001

    assert active_env_file == env_file
    assert compose_env_file == env_file
    service_env = local_service_environ({}, env_file=active_env_file)
    assert service_env["AWF_API_TOKEN"] == "operator-token"
    assert service_env["AWF_POSTGRES_PASSWORD"] == "operator-password"
    assert service_env["AWF_DOCKER_HOST"] == "unix:///tmp/awf-review.sock"
    assert service_env["AWF_ROOT_ONLY"] == "operator-only"


@pytest.mark.integration
def test_local_service_compose_port_templates_support_default_and_override_values() -> None:
    compose_path = Path("docker/compose/local-service.yml")
    data = yaml.safe_load(compose_path.read_text())
    services = data["services"]

    postgres_mapping = services["postgres"]["ports"][0]
    api_mapping = services["api"]["ports"][0]
    postgres_host = _compose_published_host_port(postgres_mapping)
    api_host = _compose_published_host_port(api_mapping)

    assert postgres_mapping == "127.0.0.1:${AWF_POSTGRES_HOST_PORT:-5433}:5432"
    assert api_mapping == "${AWF_API_HOST_PORT:-8000}:8000"

    assert _compose_template_value(postgres_host, {}) == "5433"
    assert _compose_template_value(api_host, {}) == "8000"
    assert _compose_template_value(postgres_host, {"AWF_POSTGRES_HOST_PORT": ""}) == "5433"
    assert _compose_template_value(api_host, {"AWF_API_HOST_PORT": ""}) == "8000"

    override_env = {
        "AWF_POSTGRES_HOST_PORT": "55333",
        "AWF_API_HOST_PORT": "9100",
    }
    assert _compose_template_value(postgres_host, override_env) == "55333"
    assert _compose_template_value(api_host, override_env) == "9100"


@pytest.mark.unit
def test_compose_template_value_matches_common_interpolation_forms() -> None:
    assert _compose_template_value("${AWF_BARE}", {"AWF_BARE": "resolved"}) == "resolved"
    assert _compose_template_value("${AWF_BARE}", {}) == ""
    assert (
        _compose_template_value("$${AWF_ESCAPED:-5433}", {"AWF_ESCAPED": "15433"})
        == "${AWF_ESCAPED:-5433}"
    )
    assert _compose_template_value("prefix $$ ${AWF_BARE}", {"AWF_BARE": "ok"}) == "prefix $ ok"
    assert _compose_template_value("${AWF_DEFAULT-default}", {}) == "default"
    assert _compose_template_value("${AWF_DEFAULT-default}", {"AWF_DEFAULT": ""}) == ""
    assert _compose_template_value("${AWF_DEFAULT:-default}", {"AWF_DEFAULT": ""}) == "default"
    assert (
        _compose_template_value("${AWF_BIND_IP:-127.0.0.1}:${AWF_PORT:-5433}", {})
        == "127.0.0.1:5433"
    )
    assert (
        _compose_template_value("${AWF_REQUIRED?set-AWF_REQUIRED}", {"AWF_REQUIRED": "ok"}) == "ok"
    )
    assert (
        _compose_template_value("${AWF_REQUIRED:?set AWF_REQUIRED}", {"AWF_REQUIRED": "ok"}) == "ok"
    )
    with pytest.raises(ValueError, match="AWF_REQUIRED"):
        _compose_template_value("${AWF_REQUIRED:?set AWF_REQUIRED}", {})
