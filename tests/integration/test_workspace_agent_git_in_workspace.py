"""Workspace agent container can run git status/add/commit in /workspace.

Regression coverage for the local control-plane UID/GID strategy
(see ``docs/AWF_LOCAL_CONTAINER_UID_STRATEGY.md``). The chosen default keeps
the local API/worker as root and chowns the worktree subtree, the linked
worktree git dir, and the bare-mirror admin dirs to UID/GID 1000 so the
agent-runtime user can mutate them. This test proves the rendered workspace
stack actually lets the agent user run the three git commands the acceptance
criteria call out: ``git status``, ``git add``, ``git commit``.

Skipped when:
- no Docker daemon reachable (``docker version`` fails),
- the Docker Compose plugin is unavailable,
- the ``awf-agent-runtime:latest`` image is not present locally outside CI
  (CI builds it on demand before running the regression), or
- the ``AWF_SKIP_DOCKER_TESTS=1`` env var is set.

When ``CI=true`` (set by GitHub Actions and most CI providers) the missing
image case is handled by building ``awf-agent-runtime:latest`` from the
checked-out source tree. If that build fails, the test fails loudly instead
of silently skipping the container-side contract.

The test runs as the invoking user. On Linux CI runners that user is
typically UID 1000, which matches the agent-runtime image's ``agent`` user;
the prepared worktree is then directly readable inside the container without
any chown. When the test process is privileged (UID 0), it exercises the
full root-control-plane path and chowns the worktree to UID/GID 1000
through ``GitManager`` before launching the agent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from awf.node.compose_manager import ComposeManager, WorkspaceComposeSpec
from awf.node.git_manager import GitManager

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE = _REPO_ROOT / "docker" / "compose" / "workspace.base.yml.j2"
_AGENT_IMAGE = "awf-agent-runtime:latest"
_AGENT_RUNTIME_UID = 1000
_AGENT_RUNTIME_GID = 1000


def _docker_available() -> bool:
    if os.environ.get("AWF_SKIP_DOCKER_TESTS") == "1":
        return False
    if shutil.which("docker") is None:
        return False
    for cmd in (["docker", "version"], ["docker", "compose", "version"]):
        try:
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                timeout=5,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            return False
    return True


def _agent_image_present() -> bool:
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", _AGENT_IMAGE],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return result.returncode == 0


def _running_in_ci() -> bool:
    return os.environ.get("CI", "").lower() == "true"


def _ensure_agent_image_present() -> None:
    if _agent_image_present():
        return

    msg = (
        f"{_AGENT_IMAGE} not built locally; build it with "
        "`docker build -t awf-agent-runtime:latest -f docker/agent-runtime.Dockerfile .`"
    )
    if not _running_in_ci():
        pytest.skip(msg)

    result = _run(
        [
            "docker",
            "build",
            "-t",
            _AGENT_IMAGE,
            "-f",
            "docker/agent-runtime.Dockerfile",
            ".",
        ],
        timeout=900,
        check=False,
        cwd=_REPO_ROOT,
    )
    if result.returncode != 0:
        pytest.fail(
            "CI could not build awf-agent-runtime:latest for the workspace "
            "git contract test.\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


pytestmark = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker daemon or Compose plugin not available; set AWF_SKIP_DOCKER_TESTS=1 to force-skip.",
)


def _run(
    cmd: list[str],
    *,
    timeout: int = 60,
    check: bool = True,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
    )


def _seed_origin_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    _run(["git", "init", "-q", "-b", "development", str(repo)])
    _run(["git", "-C", str(repo), "config", "user.name", "AWF Test"])
    _run(["git", "-C", str(repo), "config", "user.email", "awf@test.local"])
    (repo / "README.md").write_text("first\n")
    _run(["git", "-C", str(repo), "add", "."])
    _run(["git", "-C", str(repo), "commit", "-q", "-m", "init"])


@pytest.mark.integration
@pytest.mark.docker
@pytest.mark.slow
@pytest.mark.timeout(180)
async def test_agent_container_can_git_status_add_commit_in_workspace(
    tmp_path: Path,
) -> None:
    _ensure_agent_image_present()

    is_root = os.geteuid() == 0
    if not is_root and os.geteuid() != _AGENT_RUNTIME_UID:
        pytest.skip(
            "test must run as root or as UID "
            f"{_AGENT_RUNTIME_UID} so the prepared worktree is owned by the agent user"
        )

    workspace_id = f"test_agent_git_{os.getpid()}"
    origin_repo = tmp_path / "origin"
    _seed_origin_repo(origin_repo)

    git = GitManager(
        tmp_path / "awf-work" / "git",
        # When running as root the post-provision repair chowns to UID/GID
        # 1000. When running unprivileged, the chown step short-circuits
        # because ``os.geteuid() != 0`` — and the worktree files are
        # already owned by the test user (matching the agent UID on most
        # Linux CI runners) so no repair is needed.
        worktree_owner_uid=_AGENT_RUNTIME_UID,
        worktree_owner_gid=_AGENT_RUNTIME_GID,
    )
    layout = await git.add_worktree(
        workspace_id=workspace_id,
        repo_url=str(origin_repo),
        base_branch="development",
        new_branch=f"awf/{workspace_id}",
    )

    manager = ComposeManager(work_dir=tmp_path / "awf-work", template_path=_TEMPLATE)
    spec = WorkspaceComposeSpec(
        workspace_id=workspace_id,
        worktree_host_path=layout.worktree_path,
        agent_runtime_image=_AGENT_IMAGE,
        # Set author/committer identity so ``git commit`` does not fail with
        # "please tell me who you are" when the operator's host gitconfig is
        # not mounted.
        git_name="AWF Agent Test",
        git_email="agent@test.local",
        # Defaults are fine for everything else; the workspace template
        # adds the ``agent`` service unconditionally and we don't need any
        # profile services here.
    )

    project_name = spec.project_name()

    try:
        paths = await manager.up(spec, wait=True)

        # ``git status`` proves the bare-mirror metadata + linked worktree
        # git dir are readable by the agent user.
        status = _run(
            [
                "docker",
                "compose",
                "--project-name",
                project_name,
                "--file",
                str(paths.compose_file),
                "exec",
                "-T",
                "-u",
                "agent",
                "agent",
                "git",
                "status",
                "--porcelain",
            ]
        )
        assert status.stdout == ""

        # Write a sentinel and stage it. ``git add`` exercises the index +
        # objects database write path under ``mirror/objects/``.
        write = _run(
            [
                "docker",
                "compose",
                "--project-name",
                project_name,
                "--file",
                str(paths.compose_file),
                "exec",
                "-T",
                "-u",
                "agent",
                "agent",
                "sh",
                "-c",
                "echo 'agent wrote this' > AGENT_WROTE_THIS.md && git add AGENT_WROTE_THIS.md",
            ]
        )
        assert write.returncode == 0

        # ``git commit`` exercises the per-worktree HEAD update plus the
        # shared ref + lock-file write under ``mirror/refs/`` and
        # ``mirror/worktrees/<id>/``.
        commit = _run(
            [
                "docker",
                "compose",
                "--project-name",
                project_name,
                "--file",
                str(paths.compose_file),
                "exec",
                "-T",
                "-u",
                "agent",
                "agent",
                "git",
                "commit",
                "-m",
                "agent commit",
            ]
        )
        assert commit.returncode == 0
        assert "agent commit" in commit.stdout or "agent commit" in commit.stderr

        # The new commit propagates to the bare mirror's branch ref. This
        # locks the contract that the agent's commit was a real ref update
        # against the shared mirror, not a detached write the worktree
        # cannot reach.
        rev_in_worktree = _run(
            ["git", "-C", str(layout.worktree_path), "rev-parse", "HEAD"]
        ).stdout.strip()
        rev_in_mirror = _run(
            [
                "git",
                "--git-dir",
                str(layout.mirror_path),
                "rev-parse",
                f"refs/heads/{layout.branch_name}",
            ]
        ).stdout.strip()
        assert rev_in_worktree == rev_in_mirror
    finally:
        await manager.down(spec)

    ps = _run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"name=awf-{workspace_id}-",
            "--format",
            "{{.Names}}",
        ]
    )
    assert ps.stdout.strip() == ""
