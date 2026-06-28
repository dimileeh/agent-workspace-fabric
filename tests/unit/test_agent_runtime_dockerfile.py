from __future__ import annotations

from pathlib import Path

import pytest


def _agent_runtime_dockerfile() -> str:
    return Path("docker/agent-runtime.Dockerfile").read_text(encoding="utf-8")


@pytest.mark.unit
def test_agent_runtime_installs_pinned_github_cli_from_release_asset() -> None:
    dockerfile = _agent_runtime_dockerfile()

    assert "ARG GH_VERSION=2.92.0" in dockerfile
    assert (
        "ARG GH_AMD64_SHA256=8f8212b1a9cec261a8839e0893168f50d3fc70f095da257feef4229234cefdf8"
        in dockerfile
    )
    assert (
        "ARG GH_ARM64_SHA256=34d620b7c884774ed86236541535170889fda0b99aafbdab8b69c7d458b5ca6b"
        in dockerfile
    )
    assert "github.com/cli/cli/releases/download/v${GH_VERSION}" in dockerfile
    assert "gh_${GH_VERSION}_linux_${gh_arch}.deb" in dockerfile
    assert "curl --fail --show-error --location" in dockerfile
    assert "--retry 5" in dockerfile
    assert "--retry-delay 2" in dockerfile
    assert "--retry-all-errors" in dockerfile
    assert "--connect-timeout 20" in dockerfile
    assert "--max-time 300" in dockerfile
    assert "gh_${GH_VERSION}_checksums.txt" not in dockerfile
    assert 'expected_hash="${GH_AMD64_SHA256}"' in dockerfile
    assert 'expected_hash="${GH_ARM64_SHA256}"' in dockerfile
    assert 'awk -v asset="$gh_asset"' not in dockerfile
    assert 'actual_hash="$(sha256sum "$gh_deb")"' in dockerfile
    assert 'actual_hash="${actual_hash%% *}"' in dockerfile
    assert "GitHub CLI checksum is not pinned for ${gh_asset}" in dockerfile
    assert 'if [ "$actual_hash" != "$expected_hash" ]; then' in dockerfile
    assert "GitHub CLI checksum mismatch for ${gh_asset}" in dockerfile
    assert "grep -F" not in dockerfile
    assert "sha256sum -c -" not in dockerfile
    assert "amd64) expected_hash=" in dockerfile
    assert "arm64) expected_hash=" in dockerfile
    assert dockerfile.index('case "$gh_arch" in') < dockerfile.index(
        'actual_hash="$(sha256sum "$gh_deb")"'
    )
    assert dockerfile.index('if [ "$actual_hash" != "$expected_hash" ]; then') < dockerfile.index(
        'apt-get install -y --no-install-recommends "$gh_deb"'
    )
    assert 'apt-get install -y --no-install-recommends "$gh_deb"' in dockerfile
    assert "gh --version" in dockerfile


@pytest.mark.unit
def test_agent_runtime_installs_docker_cli_from_official_apt_repository() -> None:
    dockerfile = _agent_runtime_dockerfile()

    assert "download.docker.com/linux/debian" in dockerfile
    assert "docker.asc" in dockerfile
    assert "ARG DOCKER_CE_CLI_VERSION=" in dockerfile
    assert '"docker-ce-cli=${DOCKER_CE_CLI_VERSION}"' in dockerfile
    assert "docker --version" in dockerfile


@pytest.mark.unit
def test_agent_runtime_installs_docker_compose_plugin() -> None:
    dockerfile = _agent_runtime_dockerfile()

    assert "ARG DOCKER_COMPOSE_PLUGIN_VERSION=" in dockerfile
    assert '"docker-compose-plugin=${DOCKER_COMPOSE_PLUGIN_VERSION}"' in dockerfile
    assert "docker compose version" in dockerfile


@pytest.mark.unit
def test_agent_runtime_installs_pinned_docker_buildx_plugin() -> None:
    dockerfile = _agent_runtime_dockerfile()

    assert "ARG DOCKER_BUILDX_PLUGIN_VERSION=" in dockerfile
    assert "ARG DOCKER_BUILDX_PLUGIN_VERSION=latest" not in dockerfile
    assert '"docker-buildx-plugin=${DOCKER_BUILDX_PLUGIN_VERSION}"' in dockerfile
    assert "docker buildx version" in dockerfile


@pytest.mark.unit
def test_agent_runtime_installs_all_supported_coding_clis() -> None:
    dockerfile = _agent_runtime_dockerfile()

    assert "ARG CODEX_VERSION=0.130.0" in dockerfile
    assert "ARG CLAUDE_CODE_VERSION=2.1.158" in dockerfile
    assert "ARG GEMINI_VERSION=0.42.0" in dockerfile
    assert "ARG OPENCODE_VERSION=1.15.2" in dockerfile
    assert "Cursor CLI tracks the official installer" in dockerfile
    assert "ARG GROK_VERSION=0.2.14" in dockerfile
    assert "ARG CODEX_VERSION=latest" not in dockerfile
    assert "ARG CLAUDE_CODE_VERSION=latest" not in dockerfile
    assert "ARG GEMINI_VERSION=latest" not in dockerfile
    assert "ARG OPENCODE_VERSION=latest" not in dockerfile
    assert "ARG GROK_VERSION=latest" not in dockerfile
    assert "@openai/codex@${CODEX_VERSION}" in dockerfile
    assert "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" in dockerfile
    assert "@google/gemini-cli@${GEMINI_VERSION}" in dockerfile
    assert "opencode-ai@${OPENCODE_VERSION}" in dockerfile
    assert 'ln -sf "$(readlink -f "$(command -v node)")" /usr/local/bin/node' in dockerfile
    assert 'ln -sf "$(command -v node)" /usr/local/bin/node' not in dockerfile
    assert "cursor-agent" in dockerfile
    assert "install -d -m 0755 /opt/cursor" in dockerfile
    assert "curl https://cursor.com/install -fsS | HOME=/opt/cursor bash" in dockerfile
    assert 'cursor_path="/opt/cursor/.local/bin/cursor-agent"' in dockerfile
    assert 'ln -sf "$cursor_path" /usr/local/bin/cursor-agent' in dockerfile
    assert 'install -m 0755 "$cursor_path" /usr/local/bin/cursor-agent' not in dockerfile
    assert "chmod -R a+rX /opt/cursor" in dockerfile
    assert 'cp "$cursor_path" /usr/local/bin/cursor-agent' not in dockerfile
    assert "chmod +x /usr/local/bin/cursor-agent" in dockerfile
    assert "test -x /usr/local/bin/cursor-agent" in dockerfile
    assert "cursor-agent --version || true" in dockerfile
    assert dockerfile.index(
        'ln -sf "$(readlink -f "$(command -v node)")" /usr/local/bin/node'
    ) < dockerfile.index("curl https://cursor.com/install -fsS")
    assert (
        "USER agent\n"
        "WORKDIR /workspace\n\n"
        "RUN set -eux; \\\n"
        "    command -v cursor-agent; \\\n"
        "    test -x /usr/local/bin/cursor-agent; \\\n"
        "    cursor-agent --version"
    ) in dockerfile
    assert "npm install -g cursor-agent" not in dockerfile
    assert "@xai-official/grok@${GROK_VERSION}" in dockerfile
    assert "https://x.ai/cli/install.sh" not in dockerfile
    assert "GROK_BIN_DIR=/usr/local/bin" not in dockerfile
    assert 'bash -s "${GROK_VERSION}"' not in dockerfile
    assert "superagent-ai/grok-cli" not in dockerfile
    assert "npm install -g grok" not in dockerfile
    assert "codex --version" in dockerfile
    assert "claude --version" in dockerfile
    assert "cursor-agent --version" in dockerfile
    assert "gemini --version" in dockerfile
    assert "opencode --version" in dockerfile
    assert "grok --version" in dockerfile


@pytest.mark.unit
def test_agent_runtime_prepares_writable_cursor_config_home() -> None:
    dockerfile = _agent_runtime_dockerfile()

    assert "mkdir -p /workspace /home/agent/.config/cursor" in dockerfile
    assert "chown -R agent:agent /workspace /home/agent/.config" in dockerfile
    assert dockerfile.index("mkdir -p /workspace /home/agent/.config/cursor") < dockerfile.index(
        "USER agent"
    )


@pytest.mark.unit
def test_agent_runtime_links_gemini_bundled_ripgrep() -> None:
    dockerfile = _agent_runtime_dockerfile()

    assert "vendor/ripgrep" in dockerfile
    assert "rg-${rg_platform}-${rg_arch}" in dockerfile


@pytest.mark.unit
def test_readme_notes_agent_runtime_rebuild_for_docker_tooling_changes() -> None:
    readme = Path("CONTRIBUTING.md").read_text(encoding="utf-8")
    start = readme.index("### Build the Agent Runtime Image")
    end = readme.index("### Database Migrations")
    section = readme[start:end]

    assert "Docker CLI" in section
    assert "Docker Compose plugin" in section
    assert "buildx" in section.lower()
    assert "rebuild" in section.lower()
    assert "docker build -t awf-agent-runtime:latest" in section
