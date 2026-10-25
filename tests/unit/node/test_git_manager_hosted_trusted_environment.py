"""Trusted Git controls survive scoped hosted credentials without arbitrary merges."""

import json
import os
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

from awf.node.git_manager import GitManager
from awf.node.git_manager_ownership import git_env_for_trusted_base_materialization
from tests.unit.node.test_provisioner_parts._adoption_trusted_base_helpers import (
    origin_stale_head as origin_stale_head,
)


@pytest.fixture
def scoped_env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "GH_TOKEN": "synthetic-scoped",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }


@pytest.mark.parametrize("hosted", [False, True])
async def test_trusted_commit_ignores_stale_replace_refs_with_hosted_environment(
    tmp_path, scoped_env, hosted
):
    repo = tmp_path / "origin"
    repo.mkdir()
    base = {
        **scoped_env,
        "GIT_AUTHOR_NAME": "Synthetic",
        "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
        "GIT_COMMITTER_NAME": "Synthetic",
        "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
    }

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], env=base, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("commit", "--allow-empty", "-qm", "trusted")
    sha = git("rev-parse", "HEAD")
    refs = repo / ".git/refs/replace"
    refs.mkdir()
    (refs / sha).write_text("1" * 40 + "\n")
    manager = GitManager(
        tmp_path / "work",
        command_environment=AsyncMock(return_value=scoped_env) if hosted else None,
    )
    result = await manager._run(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"{sha}^{{commit}}"],
        operation="trusted-commit",
        env=git_env_for_trusted_base_materialization(base),
    )
    assert result.stdout.strip() == sha


async def test_only_fixed_trusted_overrides_merge_into_scoped_environment(tmp_path, scoped_env):
    callback = AsyncMock(return_value=scoped_env)
    manager = GitManager(tmp_path / "work", command_environment=callback)
    caller = git_env_for_trusted_base_materialization({})
    caller.update(
        {
            "GH_TOKEN": "synthetic-foreign",
            "HOME": "/foreign",
            "PATH": "/foreign",
            "DOCKER_HOST": "unix:///foreign",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "foreign",
            "GIT_OBJECT_DIRECTORY": "/foreign",
        }
    )
    result = await manager._run(
        [sys.executable, "-c", "import json,os;print(json.dumps(dict(os.environ)))"],
        operation="test-environment",
        env=caller,
    )
    actual = json.loads(result.stdout)
    for key, value in git_env_for_trusted_base_materialization({}).items():
        assert actual[key] == value
    for key, value in scoped_env.items():
        assert actual[key] == value
    for key in (
        caller.keys() - scoped_env.keys() - git_env_for_trusted_base_materialization({}).keys()
    ):
        assert key not in actual
    assert callback.await_count == 1
    assert scoped_env == callback.return_value


@pytest.mark.parametrize("key", list(git_env_for_trusted_base_materialization({})))
async def test_noncanonical_override_is_never_adopted(tmp_path, scoped_env, key):
    manager = GitManager(tmp_path / "work", command_environment=AsyncMock(return_value=scoped_env))
    result = await manager._run(
        [sys.executable, "-c", "import json,os;print(json.dumps(dict(os.environ)))"],
        operation="test-environment",
        env={key: "untrusted"},
    )
    actual = json.loads(result.stdout)
    assert actual.get(key) == scoped_env.get(key)


async def test_detached_trusted_base_keeps_scoped_credentials_and_raw_profile(
    tmp_path, scoped_env, origin_stale_head, monkeypatch
):
    origin, trusted, _ = origin_stale_head
    manager = GitManager(tmp_path / "work", command_environment=AsyncMock(return_value=scoped_env))
    ensure = manager.ensure_mirror

    async def poisoned_mirror(repo_url):
        mirror = await ensure(repo_url)
        refs = mirror / "refs/replace"
        refs.mkdir(exist_ok=True)
        (refs / trusted).write_text("1" * 40 + "\n")
        return mirror

    monkeypatch.setattr(manager, "ensure_mirror", poisoned_mirror)
    layout = await manager.add_detached_worktree_at_commit(
        workspace_id="ws_trusted", repo_url=str(origin), commit_sha=trusted
    )
    text = (layout.worktree_path / ".awf/workspace.yml").read_text()
    assert "name: base-safe" in text and "name: head-unsafe" not in text
