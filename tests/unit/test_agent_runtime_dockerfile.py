from __future__ import annotations

import re
from pathlib import Path

import pytest


def _agent_runtime_dockerfile() -> str:
    return Path("docker/agent-runtime.Dockerfile").read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    begin = text.index(start)
    return text[begin : text.index(end, begin)]


def _gh_block(dockerfile: str) -> str:
    return _between(dockerfile, "ARG GH_VERSION=", "# ── Stage 4")


def _agy_block(dockerfile: str) -> str:
    return _between(dockerfile, "# Install a pinned Antigravity CLI", "# Grok's ACP probe")


@pytest.mark.unit
def test_agent_runtime_installs_pinned_github_cli_from_release_asset() -> None:
    dockerfile = _agent_runtime_dockerfile()

    assert "ARG GH_VERSION=2.102.0" in dockerfile
    assert (
        "ARG GH_AMD64_SHA256=7e54a307f90afdc59796c325ec0c49fb09e6c18537727207a8ac7513584ea5b0"
        in dockerfile
    )
    assert (
        "ARG GH_ARM64_SHA256=5006962696f01e1624b3fcf1f9d8e1a11547f24bf067dd2a0371b7b421945237"
        in dockerfile
    )
    assert "github.com/cli/cli/releases/download/v${GH_VERSION}" in dockerfile
    assert "gh_${GH_VERSION}_linux_${gh_arch}.deb" in dockerfile
    assert "curl ${GITHUB_RELEASE_CURL_OPTS}" in _gh_block(dockerfile)
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
def test_github_release_downloads_share_one_retry_policy() -> None:
    """A multi-minute release CDN 504 outage must not fail the image build."""
    dockerfile = _agent_runtime_dockerfile()

    arg_lines = [
        line for line in dockerfile.splitlines() if line.startswith("ARG GITHUB_RELEASE_CURL_OPTS=")
    ]
    assert len(arg_lines) == 1
    for flag in (
        "--fail",
        "--show-error",
        "--location",
        "--retry 10",
        "--retry-all-errors",
        "--retry-max-time 900",
        "--connect-timeout 20",
        "--max-time 300",
    ):
        assert flag in arg_lines[0]
    # No fixed delay: curl then backs off exponentially between attempts.
    assert "--retry-delay" not in arg_lines[0]

    for block in (_gh_block(dockerfile), _agy_block(dockerfile)):
        assert "curl ${GITHUB_RELEASE_CURL_OPTS}" in block
        assert "--retry" not in block
        assert "--retry-delay" not in block
        assert "--max-time" not in block

    # Every GitHub-release URL is held in a variable that is fetched by a curl
    # call using the shared options.
    release_url_vars = re.findall(
        r'^\s*(\w+)="https://github\.com/[^"]*/releases/download/', dockerfile, re.MULTILINE
    )
    assert sorted(release_url_vars) == ["agy_url", "gh_primary_url"]
    for name in release_url_vars:
        assert re.search(r'curl \${GITHUB_RELEASE_CURL_OPTS} [^;]*"\$' + name + '"', dockerfile), (
            name
        )
    assert dockerfile.count("/releases/download/") == len(release_url_vars)


@pytest.mark.unit
def test_agent_runtime_github_cli_falls_back_to_pinned_official_apt_pool() -> None:
    dockerfile = _agent_runtime_dockerfile()
    block = _gh_block(dockerfile)

    primary = (
        'gh_primary_url="https://github.com/cli/cli/releases/download/v${GH_VERSION}/${gh_asset}"'
    )
    fallback = 'gh_fallback_url="https://cli.github.com/packages/pool/main/g/gh/${gh_pool_file}"'
    assert primary in block
    assert fallback in block
    # Same pinned GH_VERSION and dpkg arch on both sources, but the pool names its
    # files without the release asset's ``linux_`` infix (the bare asset name 404s).
    assert 'gh_asset="gh_${GH_VERSION}_linux_${gh_arch}.deb"' in block
    assert 'gh_pool_file="gh_${GH_VERSION}_${gh_arch}.deb"' in block
    assert 'gh_arch="$(dpkg --print-architecture)"' in block
    assert "pool/main/g/gh/${gh_asset}" not in block
    assert "linux_" not in block.split('gh_pool_file="', 1)[1].split('"', 1)[0]

    primary_download = block.index('"$gh_primary_url"; then')
    fallback_download = block.index('"$gh_fallback_url"; then')
    checksum = block.index('actual_hash="$(sha256sum "$gh_deb")"')
    mismatch = block.index('if [ "$actual_hash" != "$expected_hash" ]; then')
    install = 'apt-get install -y --no-install-recommends "$gh_deb"'
    assert block.index(primary) < block.index(fallback) < primary_download < fallback_download
    assert fallback_download < checksum < mismatch < block.index(install)
    # Exactly one checksum gate and one install, shared by both sources.
    assert block.count("sha256sum") == 1
    assert block.count("apt-get install") == 1
    assert block.count("curl ${GITHUB_RELEASE_CURL_OPTS}") == 2

    # The apt index only lists the newest gh, so no apt-managed or unverified
    # installs by name: only the checksum-verified .deb is ever installed.
    assert "apt-get download" not in block
    assert "gh=${GH_VERSION}" not in block
    assert "apt-get update" not in block
    assert "sources.list.d" not in block
    # A checksum mismatch is an integrity failure, not an outage: no fall-through.
    assert block.index("checksum mismatch") > checksum

    installed = 'gh_installed="$(gh --version)"'
    assert installed in block
    assert 'case "$gh_installed" in' in block
    assert '"gh version ${GH_VERSION} "*) ;;' in block
    assert block.index(install) < block.index(installed)


@pytest.mark.unit
def test_agent_runtime_download_failures_name_asset_and_sources_and_fail_closed() -> None:
    dockerfile = _agent_runtime_dockerfile()
    gh_block = _gh_block(dockerfile)
    agy_block = _agy_block(dockerfile)

    gh_message = (
        "GitHub CLI ${gh_asset} could not be downloaded. Sources tried: "
        "1) ${gh_primary_url} 2) ${gh_fallback_url}"
    )
    assert gh_message in gh_block
    assert gh_block.index(gh_message) < gh_block.index("exit 1", gh_block.index(gh_message))
    assert gh_block.index('"$gh_primary_url"; then') < gh_block.index(gh_message)

    agy_url = (
        "https://github.com/google-antigravity/antigravity-cli/releases/download/"
        "${ANTIGRAVITY_VERSION}/${agy_asset}"
    )
    assert f'agy_url="{agy_url}"' in agy_block
    agy_message = (
        "Antigravity CLI ${agy_asset} could not be downloaded. Sources tried: "
        "1) ${agy_url} (no official non-GitHub source exists for ${ANTIGRAVITY_VERSION})"
    )
    assert agy_message in agy_block
    assert agy_block.index(agy_message) < agy_block.index("exit 1", agy_block.index(agy_message))
    # Only the pinned release asset is fetched; no invented mirror.
    assert agy_block.count("curl ") == 1
    assert agy_block.index('actual_hash="$(sha256sum "$agy_archive")"') < agy_block.index(
        'tar -xzf "$agy_archive"'
    )


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
def test_agent_runtime_verifies_pinned_cursor_release_before_extracting() -> None:
    dockerfile = _agent_runtime_dockerfile()

    assert "ARG CURSOR_VERSION=2026.10.01-e373342" in dockerfile
    assert (
        "ARG CURSOR_X64_SHA256=a79726c6e644520e993970be4c45775a6889802b67abe461a677a53219ae28e8"
        in dockerfile
    )
    assert (
        "ARG CURSOR_ARM64_SHA256=785c5f6bf2a60eb1121e27ed8c14f5ee07ed1b5b6692324f2d9a997238245eb5"
        in dockerfile
    )
    assert "https://cursor.com/install" not in dockerfile
    assert 'cursor_dpkg_arch="$(dpkg --print-architecture)"' in dockerfile
    assert 'amd64) cursor_arch="x64"; expected_hash="${CURSOR_X64_SHA256}"' in dockerfile
    assert 'arm64) cursor_arch="arm64"; expected_hash="${CURSOR_ARM64_SHA256}"' in dockerfile
    assert "Unsupported Cursor CLI architecture: $cursor_dpkg_arch" in dockerfile
    assert (
        "https://downloads.cursor.com/lab/${CURSOR_VERSION}/linux/"
        "${cursor_arch}/agent-cli-package.tar.gz"
    ) in dockerfile
    assert (
        'cursor_archive="/tmp/cursor-agent-${CURSOR_VERSION}-${cursor_arch}.tar.gz"' in dockerfile
    )
    assert "trap 'rm -f \"$cursor_archive\"' EXIT" in dockerfile
    assert 'actual_hash="$(sha256sum "$cursor_archive")"' in dockerfile
    assert 'actual_hash="${actual_hash%% *}"' in dockerfile
    assert 'if [ "$actual_hash" != "$expected_hash" ]; then' in dockerfile
    assert "Cursor CLI checksum mismatch for ${CURSOR_VERSION}/${cursor_arch}" in dockerfile
    assert 'tar --strip-components=1 -xzf "$cursor_archive"' in dockerfile
    assert dockerfile.index('if [ "$actual_hash" != "$expected_hash" ]; then') < dockerfile.index(
        'tar --strip-components=1 -xzf "$cursor_archive"'
    )


@pytest.mark.unit
def test_agent_runtime_installs_all_supported_coding_clis() -> None:
    """Verify agent runtime installs all supported coding clis."""
    dockerfile = _agent_runtime_dockerfile()

    assert "ARG CODEX_VERSION=0.162.0" in dockerfile
    assert "ARG CLAUDE_CODE_VERSION=2.1.295" in dockerfile
    assert "ARG OPENCODE_VERSION=1.18.35" in dockerfile
    assert "ARG CURSOR_VERSION=2026.10.01-e373342" in dockerfile
    assert "ARG GROK_VERSION=1.0.50" in dockerfile
    assert "ARG ANTIGRAVITY_VERSION=1.3.2" in dockerfile
    assert (
        "ARG ANTIGRAVITY_AMD64_SHA256=bf8504c72097c97de77d271b160bdb10b956031cf79cf95d96cb948df161c25f"
        in dockerfile
    )
    assert (
        "ARG ANTIGRAVITY_ARM64_SHA256=f904ce9ca50ee7d2010bd8063a864ce37c31585c710af451bab66be6e4b59a74"
        in dockerfile
    )
    assert "github.com/google-antigravity/antigravity-cli/releases/download/" in dockerfile
    assert "install -m 0755 /tmp/antigravity /usr/local/bin/agy" in dockerfile
    assert "test -x /usr/local/bin/agy" in dockerfile
    assert "agy --version" in dockerfile
    assert "ARG CODEX_VERSION=latest" not in dockerfile
    assert "ARG CLAUDE_CODE_VERSION=latest" not in dockerfile
    assert "ARG GEMINI_VERSION=latest" not in dockerfile
    assert "ARG OPENCODE_VERSION=latest" not in dockerfile
    assert "ARG GROK_VERSION=latest" not in dockerfile
    assert "@openai/codex@${CODEX_VERSION}" in dockerfile
    assert "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" in dockerfile
    assert "@google/gemini-cli" not in dockerfile
    assert "opencode-ai@${OPENCODE_VERSION}" in dockerfile
    assert 'ln -sf "$(readlink -f "$(command -v node)")" /usr/local/bin/node' in dockerfile
    assert 'ln -sf "$(command -v node)" /usr/local/bin/node' not in dockerfile
    assert "cursor-agent" in dockerfile
    assert "install -d -m 0755 /opt/cursor" in dockerfile
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
    ) < dockerfile.index("ARG CURSOR_VERSION=2026.10.01-e373342")
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
    assert "opencode --version" in dockerfile
    assert "grok --version" in dockerfile
    assert "agy --version" in dockerfile
    assert (
        "USER agent\n"
        "WORKDIR /workspace\n\n"
        "RUN set -eux; \\\n"
        "    command -v cursor-agent; \\\n"
        "    test -x /usr/local/bin/cursor-agent; \\\n"
        "    cursor-agent --version; \\\n"
        "    command -v agy; \\\n"
        "    test -x /usr/local/bin/agy; \\\n"
        "    agy --version"
    ) in dockerfile


@pytest.mark.unit
def test_agent_runtime_installs_pinned_ccusage_usage_collector() -> None:
    """ccusage backs AWF's usage sampler, so its npm pin must stay explicit."""
    dockerfile = _agent_runtime_dockerfile()

    assert "ARG CCUSAGE_VERSION=20.0.26" in dockerfile
    assert "ARG CCUSAGE_VERSION=latest" not in dockerfile
    assert "ccusage@${CCUSAGE_VERSION}" in dockerfile
    assert "ccusage --version" in dockerfile


@pytest.mark.unit
def test_agent_runtime_checks_pinned_cli_adapter_contracts() -> None:
    """Verify pinned CLIs still parse the arguments used by AWF adapters."""
    dockerfile = _agent_runtime_dockerfile()

    assert "codex --version || true" not in dockerfile
    assert (
        "codex exec --dangerously-bypass-approvals-and-sandbox "
        "--model gpt-6.1-sol -c 'model_reasoning_effort=\"xhigh\"' --help >/dev/null"
    ) in dockerfile

    assert "gemini --version || true" not in dockerfile
    assert "gemini --skip-trust" not in dockerfile

    assert "grok --version || true" not in dockerfile
    assert (
        'grok -p "" --always-approve --no-alt-screen --no-auto-update '
        "--output-format plain --model grok-build --help >/dev/null"
    ) in dockerfile
    assert "agy --help >/dev/null" in dockerfile


@pytest.mark.unit
def test_agent_runtime_checks_grok_acp_stdio_contract() -> None:
    """Pin Grok upgrades to the ACP exchange used by the runtime adapter."""
    dockerfile = _agent_runtime_dockerfile()

    assert "timeout 15s env" in dockerfile
    assert "grok --always-approve --no-auto-update -m grok-build agent stdio" in dockerfile
    assert ('> "$grok_acp_output" || true; \\\n    jq -e -s --arg expected_version') in dockerfile
    assert '"method":"initialize"' in dockerfile
    assert '"method":"authenticate"' in dockerfile
    assert '"method":"session/new"' in dockerfile
    assert '"method":"session/prompt"' in dockerfile
    assert '--arg expected_version "$GROK_VERSION"' in dockerfile
    assert ".result._meta.agentVersion == $expected_version" in dockerfile
    assert ".result.protocolVersion == 1" in dockerfile
    assert '.result.authMethods[]?.id] | index("grok.com") != null' in dockerfile
    assert '"method":"authenticate","params":{"methodId":"grok.com"' in dockerfile
    assert "unsupported auth method" not in dockerfile
    # grok 1.0.50 stopped answering the unauthenticated ``session/new`` with an
    # error and instead emits ``_x.ai/session/setup`` auth notifications, so the
    # probe asserts the notification rather than a JSON-RPC error reply.
    assert (
        'any(.[]; .method == "_x.ai/session/setup" and .params.method == "session/new"'
        ' and .params.phase == "auth")'
    ) in dockerfile
    assert "Authentication required" not in dockerfile
    assert '.error.data == "unknown session id"' in dockerfile
    assert dockerfile.index("@xai-official/grok@${GROK_VERSION}") < dockerfile.index(
        '"method":"initialize"'
    )


@pytest.mark.unit
def test_agent_runtime_prepares_writable_cursor_config_home() -> None:
    """Verify agent runtime prepares writable cursor config home."""
    dockerfile = _agent_runtime_dockerfile()

    assert "mkdir -p /workspace /home/agent/.config/cursor" in dockerfile
    assert "chown -R agent:agent /workspace /home/agent/.config" in dockerfile
    assert dockerfile.index("mkdir -p /workspace /home/agent/.config/cursor") < dockerfile.index(
        "USER agent"
    )


@pytest.mark.unit
def test_agent_runtime_does_not_link_gemini_bundled_ripgrep() -> None:
    """Verify agent runtime does not link gemini bundled ripgrep."""
    dockerfile = _agent_runtime_dockerfile()

    assert "vendor/ripgrep" not in dockerfile
    assert "rg-${rg_platform}-${rg_arch}" not in dockerfile


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


@pytest.mark.unit
def test_contributing_documents_github_release_download_hardening() -> None:
    readme = Path("CONTRIBUTING.md").read_text(encoding="utf-8")
    section = readme[
        readme.index("### Build the Agent Runtime Image") : readme.index("### Database Migrations")
    ]

    assert "GITHUB_RELEASE_CURL_OPTS" in section
    assert "cli.github.com/packages" in section
    assert "Antigravity" in section
