"""Real Git evidence must survive hosted environment filtering."""

import os
import subprocess
from types import SimpleNamespace

import pytest

from awf.common.commands import AsyncioSubprocessRunner, FakeCommandRunner
from awf.hosted_node.runtime_consumers import HostedCommandRunner
from awf.node.git_manager_ownership import git_env_for_trusted_base_materialization
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
    _commit_trees_differ,
    _git_env_for_merge_safety_object_lookup,
    _head_descends_from,
)
from tests.unit.test_hosted_runtime_consumers import consumers


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Synthetic")
    git("config", "user.email", "synthetic@example.test")
    (root / "content").write_text("original\n")
    git("add", "content")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-qm", "empty")
    empty = git("rev-parse", "HEAD")
    (root / "content").write_text("changed\n")
    git("commit", "-qam", "contentful")
    changed = git("rev-parse", "HEAD")
    return root, git, base, empty, changed


async def test_empty_descendant_cannot_forge_fixed_content_with_replace_ref(
    repository, tmp_path, monkeypatch
):
    root, git, base, empty, changed = repository
    replacement = git("commit-tree", f"{changed}^{{tree}}", "-p", base, "-m", "replacement")
    git("replace", empty, replacement)
    # Demonstrate the poison changes ordinary Git's tree resolution.
    assert git("rev-parse", f"{base}^{{tree}}") != git("rev-parse", f"{empty}^{{tree}}")
    scope, _ = consumers(tmp_path, monkeypatch)
    target = SimpleNamespace(
        _deps=SimpleNamespace(runner=HostedCommandRunner(scope, AsyncioSubprocessRunner()))
    )
    async with scope.scope("ws_exact"):
        assert await _head_descends_from(
            target, worktree_path=root, ancestor=base, descendant=empty
        )
        assert not await _commit_trees_differ(target, worktree_path=root, left=base, right=empty)
        assert await _commit_trees_differ(target, worktree_path=root, left=base, right=changed)


async def test_default_grafts_cannot_forge_lateral_ancestry(repository, tmp_path, monkeypatch):
    root, git, base, empty, _ = repository
    lateral = git("commit-tree", f"{base}^{{tree}}", "-m", "unrelated")
    (root / ".git/info/grafts").write_text(f"{lateral} {empty}\n")
    git("merge-base", "--is-ancestor", empty, lateral)
    scope, _ = consumers(tmp_path, monkeypatch)
    target = SimpleNamespace(
        _deps=SimpleNamespace(runner=HostedCommandRunner(scope, AsyncioSubprocessRunner()))
    )
    async with scope.scope("ws_exact"):
        assert not await _head_descends_from(
            target, worktree_path=root, ancestor=empty, descendant=lateral
        )


@pytest.mark.parametrize("streaming", [False, True])
async def test_trusted_base_materialization_reads_real_blob_not_replacement(
    repository, tmp_path, monkeypatch, streaming
):
    root, git, base, empty, changed = repository
    replacement = git("commit-tree", f"{changed}^{{tree}}", "-p", base, "-m", "replacement")
    git("replace", empty, replacement)
    assert git("show", f"{empty}:content") == "changed"
    scope, _ = consumers(tmp_path, monkeypatch)
    runner = HostedCommandRunner(scope, AsyncioSubprocessRunner())
    async with scope.scope("ws_exact"):
        method = runner.run_streaming if streaming else runner.run
        result = await method(
            ["git", "-C", str(root), "show", f"{empty}:content"],
            env=git_env_for_trusted_base_materialization(),
        )
    assert result.ok and result.stdout == "original\n"


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "builder", [_git_env_for_merge_safety_object_lookup, git_env_for_trusted_base_materialization]
)
async def test_only_exact_trusted_safety_environment_survives(
    tmp_path, monkeypatch, streaming, builder
):
    scope, _ = consumers(tmp_path, monkeypatch)
    delegate = FakeCommandRunner()
    supplied = builder()
    supplied.update(
        {
            "HOME": "/foreign/home",
            "GH_TOKEN": "foreign-token",
            "CODEX_API_KEY": "foreign-key",
            "DOCKER_HOST": "tcp://foreign:2375",
            "GIT_CONFIG_COUNT": "99",
            "GIT_CONFIG_KEY_88": "credential.helper",
            "GIT_CONFIG_VALUE_88": "foreign-helper",
            "GIT_CONFIG_PARAMETERS": "foreign",
            "GIT_OBJECT_DIRECTORY": "/foreign/objects",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/foreign/alternate",
            "GIT_REPLACE_REF_BASE": "refs/foreign",
            "GIT_SSH_COMMAND": "foreign-command",
            "GIT_DIR": "/foreign/repository",
            "GIT_WORK_TREE": "/foreign/tree",
        }
    )
    runner = HostedCommandRunner(scope, delegate)
    async with scope.scope("ws_exact"):
        method = runner.run_streaming if streaming else runner.run
        await method(["git", "status"], env=supplied)
    actual = delegate.calls[0].env
    assert actual["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert actual["GIT_GRAFT_FILE"] == os.devnull
    if builder is git_env_for_trusted_base_materialization:
        assert actual["GIT_ATTR_NOSYSTEM"] == "1"
        assert actual["GIT_CONFIG_SYSTEM"] == os.devnull
    assert actual["HOME"] == str(tmp_path / "hosted-empty-home")
    assert actual["GH_TOKEN"] == "scoped-github"
    assert actual["CODEX_API_KEY"] == "scoped-agent"
    assert actual["GIT_CONFIG_COUNT"] == "2"
    assert not any("foreign" in value for value in actual.values())


@pytest.mark.parametrize(
    "key,value",
    [
        ("GIT_NO_REPLACE_OBJECTS", "0"),
        ("GIT_NO_REPLACE_OBJECTS", "true"),
        ("GIT_GRAFT_FILE", "/foreign/grafts"),
        ("GIT_ATTR_NOSYSTEM", "0"),
        ("GIT_CONFIG_SYSTEM", "/foreign/config"),
    ],
)
async def test_noncanonical_git_safety_values_stay_denied(tmp_path, monkeypatch, key, value):
    scope, _ = consumers(tmp_path, monkeypatch)
    delegate = FakeCommandRunner()
    async with scope.scope("ws_exact"):
        await HostedCommandRunner(scope, delegate).run(["git", "status"], env={key: value})
    assert key not in delegate.calls[0].env


@pytest.mark.parametrize("command", [["gh", "api", "graphql"], ["docker", "inspect", "synthetic"]])
async def test_git_safety_exception_is_not_a_general_environment_allowlist(
    tmp_path, monkeypatch, command
):
    scope, _ = consumers(tmp_path, monkeypatch)
    delegate = FakeCommandRunner()
    async with scope.scope("ws_exact"):
        await HostedCommandRunner(scope, delegate).run(
            command, env=_git_env_for_merge_safety_object_lookup()
        )
    assert "GIT_NO_REPLACE_OBJECTS" not in delegate.calls[0].env
    assert "GIT_GRAFT_FILE" not in delegate.calls[0].env
