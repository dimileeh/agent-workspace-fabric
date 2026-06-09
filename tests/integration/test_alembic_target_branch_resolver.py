"""Integration coverage for AWF's post-merge Alembic target-branch resolver."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from awf.common.commands import AsyncioSubprocessRunner
from awf.service.alembic_resolver import AlembicResolveStatus
from awf.service.target_branch_monitor import (
    TargetBranchMonitorStatus,
    TargetBranchReconcileMonitor,
)


def _run(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run(["git", "-C", str(repo), *args])


def _git_dir(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run(["git", "--git-dir", str(repo), *args])


def _configure_git_user(repo: Path) -> None:
    _git(repo, "config", "user.name", "AWF Test")
    _git(repo, "config", "user.email", "awf-test@example.invalid")


def _write_alembic_project(repo: Path) -> None:
    (repo / "migrations" / "versions").mkdir(parents=True)
    (repo / "alembic.ini").write_text(
        "[alembic]\n"
        "script_location = migrations\n"
        "prepend_sys_path = ./src\n"
        "path_separator = os\n"
        "version_path_separator = os\n",
        encoding="utf-8",
    )


def _write_revision(
    repo: Path,
    revision: str,
    down_revision: str | None,
    *,
    slug: str | None = None,
) -> None:
    down_revision_literal = "None" if down_revision is None else repr(down_revision)
    (repo / "migrations" / "versions" / f"{revision}_{slug or revision}.py").write_text(
        f'"""Revision {revision}."""\n\n'
        f'revision = "{revision}"\n'
        f"down_revision = {down_revision_literal}\n"
        "branch_labels = None\n"
        "depends_on = None\n\n\n"
        "def upgrade() -> None:\n"
        "    pass\n\n\n"
        "def downgrade() -> None:\n"
        "    pass\n",
        encoding="utf-8",
    )


def _heads(repo: Path) -> list[str]:
    config = Config(str(repo / "alembic.ini"))
    config.set_main_option("path_separator", "os")
    config.set_main_option("script_location", str(repo / "migrations"))
    return sorted(ScriptDirectory.from_config(config).get_heads())


def _origin_head(origin: Path, branch: str = "development") -> str:
    return _git_dir(origin, "rev-parse", branch).stdout.strip()


def _seed_origin(tmp_path: Path, *, single_head: bool) -> Path:
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    _run(["git", "init", "--bare", str(origin)])
    _run(["git", "init", "--initial-branch", "development", str(seed)])
    _configure_git_user(seed)
    _write_alembic_project(seed)
    _write_revision(seed, "base001", None)
    if single_head:
        _write_revision(seed, "head001", "base001")
    _git(seed, "add", "alembic.ini", "migrations")
    _git(seed, "commit", "-m", "feat(migrations): seed alembic graph")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", "development")
    _git_dir(origin, "symbolic-ref", "HEAD", "refs/heads/development")
    return origin


def _push_independent_revision(
    tmp_path: Path,
    origin: Path,
    *,
    clone_name: str,
    branch: str,
    revision: str,
) -> None:
    clone = tmp_path / clone_name
    _run(["git", "clone", "--branch", "development", str(origin), str(clone)])
    _configure_git_user(clone)
    _git(clone, "checkout", "-b", branch)
    _write_revision(clone, revision, "base001")
    _git(clone, "add", "migrations")
    _git(clone, "commit", "-m", f"feat(migrations): add {revision}")
    _git(clone, "push", "origin", f"HEAD:{branch}")


def _merge_independent_branches(tmp_path: Path, origin: Path) -> None:
    integrator = tmp_path / "integrator"
    _run(["git", "clone", "--branch", "development", str(origin), str(integrator)])
    _configure_git_user(integrator)
    _git(integrator, "fetch", "origin")
    _git(integrator, "merge", "--no-ff", "origin/pr-left", "-m", "Merge PR left")
    _git(integrator, "merge", "--no-ff", "origin/pr-right", "-m", "Merge PR right")
    assert _heads(integrator) == ["left001", "right001"]
    _git(integrator, "push", "origin", "development")


def _expected_default_revision(heads: tuple[str, ...]) -> str:
    digest = hashlib.sha1(",".join(sorted(heads)).encode("utf-8")).hexdigest()[:12]
    return f"awf_merge_{digest}"


@pytest.mark.integration
async def test_target_branch_reconcile_single_alembic_head_is_noop(tmp_path: Path) -> None:
    origin = _seed_origin(tmp_path, single_head=True)
    head_before = _origin_head(origin)
    monitor = TargetBranchReconcileMonitor(
        runner=AsyncioSubprocessRunner(),
        work_dir=tmp_path / "awf-state",
    )

    result = await monitor.reconcile(repo_url=str(origin), branch="development")

    assert result.status == TargetBranchMonitorStatus.clean
    assert result.commit_sha is None
    assert result.pushed is False
    assert _origin_head(origin) == head_before
    assert len(result.resolver_results) == 1
    resolver_result = result.resolver_results[0]
    assert resolver_result.status == AlembicResolveStatus.not_needed
    assert resolver_result.reason_code == "ALEMBIC_SINGLE_HEAD"
    assert resolver_result.heads == ("head001",)
    assert resolver_result.generated_path is None
    assert _heads(result.checkout_path) == ["head001"]
    assert not list(
        (result.checkout_path / "migrations" / "versions").glob("*merge_alembic_heads.py")
    )


@pytest.mark.integration
async def test_target_branch_reconcile_merges_independent_alembic_heads_and_pushes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "AWF Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "awf-test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "AWF Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "awf-test@example.invalid")
    origin = _seed_origin(tmp_path, single_head=False)
    _push_independent_revision(
        tmp_path,
        origin,
        clone_name="left",
        branch="pr-left",
        revision="left001",
    )
    _push_independent_revision(
        tmp_path,
        origin,
        clone_name="right",
        branch="pr-right",
        revision="right001",
    )
    _merge_independent_branches(tmp_path, origin)
    before_reconcile = _origin_head(origin)
    expected_heads = ("left001", "right001")
    expected_revision = _expected_default_revision(expected_heads)
    expected_path = f"migrations/versions/{expected_revision}_merge_alembic_heads.py"
    monitor = TargetBranchReconcileMonitor(
        runner=AsyncioSubprocessRunner(),
        work_dir=tmp_path / "awf-state",
    )

    result = await monitor.reconcile(repo_url=str(origin), branch="development")

    assert result.status == TargetBranchMonitorStatus.committed
    assert result.commit_sha is not None
    assert result.pushed is True
    assert _origin_head(origin) == result.commit_sha
    assert _origin_head(origin) != before_reconcile
    assert result.changed_paths == (expected_path,)
    resolver_result = result.resolver_results[0]
    assert resolver_result.status == AlembicResolveStatus.resolved
    assert resolver_result.reason_code == "ALEMBIC_HEADS_MERGED"
    assert resolver_result.heads == expected_heads
    assert resolver_result.generated_revision == expected_revision
    assert resolver_result.generated_path_relative == expected_path
    assert resolver_result.generated_path is not None
    generated = resolver_result.generated_path.read_text(encoding="utf-8")
    assert f'revision = "{expected_revision}"' in generated
    assert 'down_revision = ("left001", "right001")' in generated
    assert _heads(result.checkout_path) == [expected_revision]
    log = _git_dir(origin, "log", "-1", "--format=%s%n%b", "development").stdout
    assert "fix(migrations): merge Alembic heads on development" in log
    committed_files = _git_dir(origin, "show", "--name-only", "--format=", result.commit_sha).stdout
    assert expected_path in committed_files.splitlines()
