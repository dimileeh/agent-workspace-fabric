from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.unit
def test_agent_runtime_installs_github_cli_from_official_apt_repository() -> None:
    dockerfile = Path("docker/agent-runtime.Dockerfile").read_text(encoding="utf-8")

    assert "cli.github.com/packages" in dockerfile
    assert "githubcli-archive-keyring.gpg" in dockerfile
    assert "apt-get install -y --no-install-recommends gh" in dockerfile
    assert "gh --version" in dockerfile
