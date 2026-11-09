"""Service Compose must deliver provider configuration to both control processes."""

from pathlib import Path

import pytest
import yaml

from awf.service.config import local_service_worker_environment_keys


@pytest.mark.parametrize("service", ["api", "worker"])
def test_provider_settings_reach_local_service_containers(service):
    compose = Path("docker/compose/local-service.yml")
    services = yaml.safe_load(compose.read_text())["services"]
    environment = dict(
        item.split("=", 1) for item in services[service]["environment"] if "=" in item
    )
    for name in ("OPENROUTER_API_KEY", "AWF_OLLAMA_BASE_URL"):
        assert environment[name] == "${" + name + ":-}"
        assert name in local_service_worker_environment_keys(compose_file=compose)
    assert environment["AWF_THIRD_PARTY_AGENT_PROVIDERS_ENABLED"].endswith(":-false}")
