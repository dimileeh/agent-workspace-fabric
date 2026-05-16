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
  (CI builds it on demand before running the regression),
- the ``AWF_SKIP_DOCKER_TESTS=1`` env var is set, or
- the test process is neither root nor the agent UID *and* passwordless
  sudo is not available (the test needs one of those three to land the
  prepared worktree owned by UID 1000) — but only on developer machines:
  see CI behavior below.

When ``CI=true`` (set by GitHub Actions and most CI providers) the missing
image case is handled by building ``awf-agent-runtime:latest`` from the
checked-out source tree. If that build fails, the test fails loudly instead
of silently skipping the container-side contract. The UID/sudo precondition
is also a hard failure under ``CI=true``: ubuntu-latest gives the ``runner``
user (UID 1001) passwordless sudo, so the documented path always succeeds;
if a runner-image change ever breaks that, the test fails loudly so the
workspace-stack git contract is not silently bypassed in CI.

The test runs as the invoking user and supports three modes:

- **UID 0 (root):** exercises the full root-control-plane path; ``GitManager``
  chowns the worktree to UID/GID 1000 before the agent container launches.
- **UID 1000 (agent UID, common on bespoke Linux dev hosts):** the worktree
  is created by the test process and is already owned by the agent UID, so
  no chown is needed.
- **any other UID with passwordless sudo (e.g. GitHub Actions' ``runner``
  user is UID 1001):** the test uses ``sudo chown`` to mimic what the root
  control-plane would do. This keeps the contract exercised on standard CI
  runners instead of silently skipping there.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from awf.common.git_identity import git_safe_directory_config_args
from awf.node.compose_manager import AuthMount, ComposeManager, WorkspaceComposeSpec
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


def _passwordless_sudo_available() -> bool:
    """``sudo -n true`` succeeds, i.e. sudo runs without prompting."""
    if shutil.which("sudo") is None:
        return False
    try:
        result = subprocess.run(
            ["sudo", "-n", "true"],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return result.returncode == 0


def _sudo_chown(target: Path, *, uid: int, gid: int) -> None:
    """Recursively chown ``target`` to ``uid:gid`` via passwordless sudo."""
    _run(
        ["sudo", "-n", "chown", "-R", f"{uid}:{gid}", str(target)],
        timeout=120,
    )


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
# Worst case under ``CI=true`` with a cold image cache: the in-test
# ``_ensure_agent_image_present()`` call on the first line below shells out to
# ``docker build`` with its own 900s subprocess timeout, plus the actual
# workspace-up + git-status/add/commit exec dance. The pytest-timeout marker
# wraps the whole test function, so it must comfortably exceed the docker-build
# subprocess timeout — otherwise pytest-timeout kills the test mid-build on a
# cold runner while the build subprocess keeps running, masking the contract.
@pytest.mark.timeout(1200)
async def test_agent_container_can_git_status_add_commit_in_workspace(
    tmp_path: Path,
) -> None:
    _ensure_agent_image_present()

    is_root = os.geteuid() == 0
    is_agent_uid = os.geteuid() == _AGENT_RUNTIME_UID
    needs_sudo_chown = not is_root and not is_agent_uid
    if needs_sudo_chown and not _passwordless_sudo_available():
        msg = (
            f"test must run as root, as UID {_AGENT_RUNTIME_UID}, or with "
            "passwordless sudo available so the prepared worktree can be "
            "chowned to the agent UID (GitHub Actions ubuntu-latest runs as "
            "UID 1001 with passwordless sudo, so the CI integration job "
            "exercises this path)"
        )
        # ``CI=true`` means the workspace-stack git contract MUST be
        # exercised — silently skipping would let regressions in the agent
        # readability invariant slip through. Fail loudly so a runner-image
        # change (sudo dropped, base image swap, etc.) surfaces immediately
        # instead of turning into a green-but-empty integration job.
        if _running_in_ci():
            pytest.fail(msg)
        pytest.skip(msg)

    workspace_id = f"test_agent_git_{os.getpid()}"
    origin_repo = tmp_path / "origin"
    _seed_origin_repo(origin_repo)

    git = GitManager(
        tmp_path / "awf-work" / "git",
        # When running as root the post-provision repair chowns to UID/GID
        # 1000. When running unprivileged, the chown step short-circuits
        # because ``os.geteuid() != 0``; the chown is reapplied below via
        # sudo for the non-root, non-agent-UID case so the contract is
        # still exercised on standard CI runners.
        worktree_owner_uid=_AGENT_RUNTIME_UID,
        worktree_owner_gid=_AGENT_RUNTIME_GID,
    )
    layout = await git.add_worktree(
        workspace_id=workspace_id,
        repo_url=str(origin_repo),
        base_branch="development",
        new_branch=f"awf/{workspace_id}",
    )

    if needs_sudo_chown:
        # Mimic the root control-plane chown so the agent inside the
        # container can write to the host-bind-mounted worktree. Without
        # this, ``git add`` / ``git commit`` would fail with EACCES on the
        # mirror's ``objects/`` and ``refs/`` paths and the contract this
        # test exists to lock would skip silently in CI.
        _sudo_chown(layout.mirror_path, uid=_AGENT_RUNTIME_UID, gid=_AGENT_RUNTIME_GID)
        _sudo_chown(layout.worktree_path, uid=_AGENT_RUNTIME_UID, gid=_AGENT_RUNTIME_GID)

    manager = ComposeManager(work_dir=tmp_path / "awf-work", template_path=_TEMPLATE)
    spec = WorkspaceComposeSpec(
        workspace_id=workspace_id,
        worktree_host_path=layout.worktree_path,
        agent_runtime_image=_AGENT_IMAGE,
        auth_mounts=(
            AuthMount(
                source=str(layout.mirror_path),
                target=str(layout.mirror_path),
                mode="rw",
            ),
        ),
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
                "-p",
                project_name,
                "-f",
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
                "-p",
                project_name,
                "-f",
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
                "-p",
                project_name,
                "-f",
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
        #
        # In the ``needs_sudo_chown`` branch the worktree and mirror are
        # owned by UID 1000 (so the agent container can write to them),
        # but this process runs as the invoking UID (e.g., 1001 on
        # GitHub Actions). git refuses with "dubious ownership" when the
        # repo directory's owner does not match the caller, so each
        # invocation whitelists its own path via ``-c safe.directory``.
        # Harmless in the root and UID-1000 branches.
        rev_in_worktree = _run(
            [
                "git",
                *git_safe_directory_config_args(layout.worktree_path),
                "-C",
                str(layout.worktree_path),
                "rev-parse",
                "HEAD",
            ]
        ).stdout.strip()
        rev_in_mirror = _run(
            [
                "git",
                *git_safe_directory_config_args(layout.mirror_path),
                "--git-dir",
                str(layout.mirror_path),
                "rev-parse",
                f"refs/heads/{layout.branch_name}",
            ]
        ).stdout.strip()
        assert rev_in_worktree == rev_in_mirror
    finally:
        await manager.down(spec)
        if needs_sudo_chown:
            # Best-effort: chown back to the test UID so pytest's tmp_path
            # rotation can remove the worktree + mirror tree on later runs.
            # We don't fail the test if this step errors — the next run's
            # rotation cleanup just warns.
            subprocess.run(
                [
                    "sudo",
                    "-n",
                    "chown",
                    "-R",
                    f"{os.geteuid()}:{os.getegid()}",
                    str(tmp_path),
                ],
                check=False,
                capture_output=True,
                timeout=120,
            )

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
