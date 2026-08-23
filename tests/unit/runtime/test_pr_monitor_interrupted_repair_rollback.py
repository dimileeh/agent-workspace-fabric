"""Recovery of unpublished comment-repair commits without output salvage."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import AsyncioSubprocessRunner, CommandResult
from awf.db.enums import OperationStatus, OperationType
from awf.db.models import Operation
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import remote_repair_unpublished


class _RollbackCommandRunner:
    def __init__(
        self,
        *,
        remote_head: str,
        local_head: str,
        local_behind_remote: bool = False,
        ancestry: dict[tuple[str, str], bool] | None = None,
        head_advance_after_ancestry: str | None = None,
        dirty_before_reset: bool = False,
    ) -> None:
        self.remote_head = remote_head
        self.local_head = local_head
        self.local_behind_remote = local_behind_remote
        self.ancestry = ancestry
        self.head_advance_after_ancestry = head_advance_after_ancestry
        self.dirty_before_reset = dirty_before_reset
        self._ancestry_checked = False
        self._reset_done = False
        self.calls: list[tuple[str, ...]] = []

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        if ancestor == descendant:
            return True
        if self.ancestry is not None:
            return self.ancestry.get((ancestor, descendant), False)
        return True

    async def run(self, args: list[str], **_kwargs: object) -> CommandResult:
        call = tuple(args)
        self.calls.append(call)
        if "rev-parse" in call:
            ref = call[call.index("rev-parse") + 1]
            if ref == "FETCH_HEAD":
                head = self.remote_head
            elif self._ancestry_checked and self.head_advance_after_ancestry is not None:
                head = self.head_advance_after_ancestry
            else:
                head = self.local_head
            return CommandResult(returncode=0, stdout=f"{head}\n", stderr="")
        if "merge-base" in call and "--is-ancestor" in call:
            self._ancestry_checked = True
            ancestor_ref = call[call.index("--is-ancestor") + 1]
            descendant_ref = call[call.index("--is-ancestor") + 2]
            if self.ancestry is None and self.local_behind_remote:
                remote_refs = {"FETCH_HEAD", self.remote_head}
                if ancestor_ref in remote_refs and descendant_ref == "HEAD":
                    return CommandResult(returncode=1, stdout="", stderr="")
                if ancestor_ref == "HEAD" and descendant_ref in remote_refs:
                    return CommandResult(returncode=0, stdout="", stderr="")
            ancestor = ancestor_ref
            descendant = descendant_ref
            if ancestor == "FETCH_HEAD":
                ancestor = self.remote_head
            if descendant == "FETCH_HEAD":
                descendant = self.remote_head
            if ancestor == "HEAD":
                ancestor = self.local_head
            if descendant == "HEAD":
                descendant = self.local_head
            return CommandResult(
                returncode=0 if self._is_ancestor(ancestor, descendant) else 1,
                stdout="",
                stderr="",
            )
        if "diff" in call:
            return CommandResult(returncode=0, stdout="M\0src/example.py\0", stderr="")
        if "reset" in call:
            self._reset_done = True
            self.local_head = self.remote_head
            return CommandResult(returncode=0, stdout="", stderr="")
        if "status" in call:
            if not self._reset_done and self._ancestry_checked and self.dirty_before_reset:
                return CommandResult(returncode=0, stdout=" M\0src/example.py\0", stderr="")
            return CommandResult(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {call}")


def _runner(tmp_path: Path, command_runner: _RollbackCommandRunner) -> SimpleNamespace:
    async def _fetch(**_kwargs: object) -> CommandResult:
        return CommandResult(returncode=0, stdout="", stderr="")

    return SimpleNamespace(
        _worktrees_root=tmp_path,
        _deps=SimpleNamespace(runner=command_runner),
        _remote_branch_fetch_once=_fetch,
    )


@pytest.fixture(autouse=True)
def _comment_repair_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _has_comment_provenance(*_args: object, **_kwargs: object) -> bool:
        return True

    async def _has_conflicting_provenance(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_unpublished_comment_repair_has_operation_provenance",
        _has_comment_provenance,
    )
    monkeypatch.setattr(
        remote_repair_unpublished,
        "_unpublished_non_comment_repair_has_operation_provenance",
        _has_conflicting_provenance,
    )


@pytest.fixture(autouse=True)
def _verified_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        remote_repair_unpublished,
        "_verified_awf_comment_repair_worktree",
        lambda **_kwargs: True,
        raising=False,
    )

    async def _ownership_ok(**_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(remote_repair_unpublished, "repair_agent_runtime_ownership", _ownership_ok)


@pytest.mark.unit
async def test_unpublished_descendant_is_reset_to_verified_remote_head(tmp_path: Path) -> None:
    workspace_id = "ws_interrupted"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    remote_head = "a" * 40
    abandoned_head = "b" * 40
    commands = _RollbackCommandRunner(remote_head=remote_head, local_head=abandoned_head)

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=remote_head,
        local_head=abandoned_head,
        state=MonitorState(),
    )

    assert result is None
    assert restored_head == remote_head
    assert commands.local_head == remote_head
    reset_calls = [call for call in commands.calls if "reset" in call]
    assert len(reset_calls) == 1
    assert reset_calls[0][-2:] == ("--hard", remote_head)


@pytest.mark.unit
async def test_behind_remote_head_fast_forwards_without_failure(tmp_path: Path) -> None:
    workspace_id = "ws_behind"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    remote_head = "c" * 40
    stale_local_head = "a" * 40
    commands = _RollbackCommandRunner(
        remote_head=remote_head,
        local_head=stale_local_head,
        local_behind_remote=True,
    )

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=remote_head,
        local_head=stale_local_head,
        state=MonitorState(),
    )

    assert result is None
    assert restored_head == remote_head
    assert commands.local_head == remote_head
    reset_calls = [call for call in commands.calls if "reset" in call]
    assert len(reset_calls) == 1
    assert reset_calls[0][-2:] == ("--hard", remote_head)
    assert all("diff" not in call for call in commands.calls)


@pytest.mark.unit
async def test_behind_remote_fast_forward_refuses_when_head_advances_before_reset(
    tmp_path: Path,
) -> None:
    workspace_id = "ws_behind_head_race"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    remote_head = "c" * 40
    stale_local_head = "a" * 40
    advanced_head = "d" * 40
    commands = _RollbackCommandRunner(
        remote_head=remote_head,
        local_head=stale_local_head,
        local_behind_remote=True,
        head_advance_after_ancestry=advanced_head,
    )

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=remote_head,
        local_head=stale_local_head,
        state=MonitorState(),
    )

    assert restored_head == stale_local_head
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"
    assert all("reset" not in call for call in commands.calls)


@pytest.mark.unit
async def test_unpublished_descendant_refuses_when_head_advances_before_reset(
    tmp_path: Path,
) -> None:
    workspace_id = "ws_descendant_head_race"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    remote_head = "a" * 40
    abandoned_head = "b" * 40
    advanced_head = "e" * 40
    commands = _RollbackCommandRunner(
        remote_head=remote_head,
        local_head=abandoned_head,
        head_advance_after_ancestry=advanced_head,
    )

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=remote_head,
        local_head=abandoned_head,
        state=MonitorState(),
    )

    assert restored_head == abandoned_head
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"
    assert all("reset" not in call for call in commands.calls)


@pytest.mark.unit
async def test_behind_remote_fast_forward_refuses_when_worktree_becomes_dirty_before_reset(
    tmp_path: Path,
) -> None:
    workspace_id = "ws_behind_dirty_race"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    remote_head = "c" * 40
    stale_local_head = "a" * 40
    commands = _RollbackCommandRunner(
        remote_head=remote_head,
        local_head=stale_local_head,
        local_behind_remote=True,
        dirty_before_reset=True,
    )

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=remote_head,
        local_head=stale_local_head,
        state=MonitorState(),
    )

    assert restored_head == stale_local_head
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"
    assert "dirty" in result.stderr.lower()
    assert all("reset" not in call for call in commands.calls)


@pytest.mark.unit
async def test_unpublished_descendant_refuses_when_worktree_becomes_dirty_before_reset(
    tmp_path: Path,
) -> None:
    workspace_id = "ws_descendant_dirty_race"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    remote_head = "a" * 40
    abandoned_head = "b" * 40
    commands = _RollbackCommandRunner(
        remote_head=remote_head,
        local_head=abandoned_head,
        dirty_before_reset=True,
    )

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=remote_head,
        local_head=abandoned_head,
        state=MonitorState(),
    )

    assert restored_head == abandoned_head
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"
    assert "dirty" in result.stderr.lower()
    assert all("reset" not in call for call in commands.calls)


@pytest.mark.unit
async def test_already_published_local_head_supersedes_stale_snapshot(tmp_path: Path) -> None:
    workspace_id = "ws_published"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    stale_snapshot = "a" * 40
    published_head = "b" * 40
    commands = _RollbackCommandRunner(
        remote_head=published_head,
        local_head=published_head,
    )

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=stale_snapshot,
        local_head=published_head,
        state=MonitorState(),
    )

    assert result is None
    assert restored_head == published_head
    assert any(
        "merge-base" in call and call[-3:] == ("--is-ancestor", stale_snapshot, published_head)
        for call in commands.calls
    )
    assert all("reset" not in call for call in commands.calls)


@pytest.mark.unit
async def test_stale_snapshot_remote_advance_resets_unpublished_repairs(tmp_path: Path) -> None:
    workspace_id = "ws_stale_snapshot"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    stale_snapshot = "a" * 40
    advanced_remote = "c" * 40
    unpublished_repair = "b" * 40
    commands = _RollbackCommandRunner(
        remote_head=advanced_remote,
        local_head=unpublished_repair,
        ancestry={
            (stale_snapshot, advanced_remote): True,
            (stale_snapshot, unpublished_repair): True,
            (advanced_remote, unpublished_repair): False,
            (unpublished_repair, advanced_remote): False,
        },
    )

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=stale_snapshot,
        local_head=unpublished_repair,
        state=MonitorState(),
    )

    assert result is None
    assert restored_head == advanced_remote
    assert commands.local_head == advanced_remote
    reset_calls = [call for call in commands.calls if "reset" in call]
    assert len(reset_calls) == 1
    assert reset_calls[0][-2:] == ("--hard", advanced_remote)


@pytest.mark.unit
async def test_remote_head_mismatch_fails_without_reset(tmp_path: Path) -> None:
    workspace_id = "ws_mismatch"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    expected = "a" * 40
    fetched = "c" * 40
    local = "b" * 40
    commands = _RollbackCommandRunner(
        remote_head=fetched,
        local_head=local,
        ancestry={
            (expected, fetched): True,
            (expected, local): False,
            (fetched, local): False,
            (local, fetched): False,
        },
    )

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=expected,
        local_head=local,
        state=MonitorState(),
    )

    assert restored_head == local
    assert result is not None
    assert result.failed is True
    assert result.terminal_monitor_failure is True
    assert result.reason_code == "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"
    assert all("reset" not in call for call in commands.calls)


@pytest.mark.unit
async def test_awaiting_workflow_scope_repair_is_never_reset(tmp_path: Path) -> None:
    workspace_id = "ws_workflow_scope"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    commands = _RollbackCommandRunner(remote_head="a" * 40, local_head="b" * 40)
    state = MonitorState()
    state.mark_awaiting_workflow_scope()

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head="a" * 40,
        local_head="b" * 40,
        state=state,
    )

    assert result is None
    assert restored_head == "b" * 40
    assert commands.calls == []


@pytest.mark.unit
async def test_preserved_protected_flow_is_never_reset(tmp_path: Path) -> None:
    workspace_id = "ws_preserved"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    commands = _RollbackCommandRunner(remote_head="a" * 40, local_head="b" * 40)
    state = MonitorState()
    state.mark_addressed("__awf_protected_block_preserved_head__", "b" * 40)

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head="a" * 40,
        local_head="b" * 40,
        state=state,
    )

    assert result is None
    assert restored_head == "b" * 40
    assert commands.calls == []


@pytest.mark.unit
async def test_ci_repair_unpublished_commit_is_not_reset_without_comment_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = "ws_ci_repair_unpublished"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    remote_head = "a" * 40
    ci_repair_head = "b" * 40
    commands = _RollbackCommandRunner(remote_head=remote_head, local_head=ci_repair_head)

    async def _no_comment_provenance(*_args: object, **_kwargs: object) -> bool:
        return False

    async def _ci_repair_provenance(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(
        remote_repair_unpublished,
        "_unpublished_comment_repair_has_operation_provenance",
        _no_comment_provenance,
    )
    monkeypatch.setattr(
        remote_repair_unpublished,
        "_unpublished_non_comment_repair_has_operation_provenance",
        _ci_repair_provenance,
    )

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=remote_head,
        local_head=ci_repair_head,
        state=MonitorState(),
        current_operation_id="op_comment_repair_current",
    )

    assert restored_head == ci_repair_head
    assert commands.local_head == ci_repair_head
    assert all("reset" not in call for call in commands.calls)
    assert result is not None
    assert result.failed is True
    assert result.terminal_monitor_failure is True
    assert result.reason_code == "COMMENT_REPAIR_UNPUBLISHED_PROVENANCE_MISSING"


@pytest.mark.unit
def test_operation_result_was_pushed_for_succeeded_ci_repair_outcome() -> None:
    from awf.db.enums import OperationStatus
    from awf.db.models import Operation

    operation = Operation(
        id="op_ci",
        workspace_id="ws",
        type="ci_repair",
        status=OperationStatus.succeeded.value,
        result={"outcome": "ci_repair_pushed", "pushed": True},
    )
    assert remote_repair_unpublished._operation_result_was_pushed(operation) is True


@pytest.mark.unit
def test_is_operator_hint_repair_operation_matches_comment_repair_payload_action() -> None:
    operation = Operation(
        id="op_hint",
        workspace_id="ws",
        type=OperationType.comment_repair.value,
        status=OperationStatus.running.value,
        payload={"action": "operator_hint_repair", "source_head_sha": "a" * 40},
    )
    assert remote_repair_unpublished._is_operator_hint_repair_operation(operation) is True


@pytest.mark.unit
def test_is_operator_hint_repair_operation_rejects_plain_comment_repair() -> None:
    operation = Operation(
        id="op_comment",
        workspace_id="ws",
        type=OperationType.comment_repair.value,
        status=OperationStatus.running.value,
        payload={"action": "comment_repair", "source_head_sha": "a" * 40},
    )
    assert remote_repair_unpublished._is_operator_hint_repair_operation(operation) is False


@pytest.mark.unit
async def test_non_linked_worktree_does_not_enter_rollback_path(tmp_path: Path) -> None:
    workspace_id = "ws_plain_directory"
    (tmp_path / workspace_id).mkdir()
    local_head = "b" * 40
    commands = _RollbackCommandRunner(remote_head="a" * 40, local_head=local_head)

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, commands),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head="a" * 40,
        local_head=local_head,
        state=MonitorState(),
    )

    assert result is None
    assert restored_head == local_head
    assert commands.calls == []


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _write_fetch_head(repo: Path, sha: str, *, branch: str = "fix/review") -> None:
    """Set FETCH_HEAD without ``git update-ref`` (rejected for pseudorefs since Git 2.55)."""
    git_dir = repo / ".git"
    if git_dir.is_file():
        git_dir = Path(git_dir.read_text(encoding="utf-8").split(":", 1)[1].strip())
    fetch_head = git_dir / "FETCH_HEAD"
    fetch_head.parent.mkdir(parents=True, exist_ok=True)
    fetch_head.write_text(
        f"{sha}\tnot-for-merge\tbranch '{branch}' of local test remote\n",
        encoding="utf-8",
    )


def _init_repo_with_lateral_and_remote(worktree: Path) -> tuple[Path, str, str, str]:
    """Return ``(repo, ancestor_sha, remote_sha, lateral_sha)``."""
    repo = worktree
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    _git(repo, "config", "advice.graftFileDeprecated", "false")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "ancestor")
    ancestor = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-qm", "remote tip")
    remote = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "--orphan", "lateral", "-q")
    (repo / "c.txt").write_text("c\n", encoding="utf-8")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-qm", "lateral tip")
    lateral = _git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, ancestor, remote, lateral


@pytest.mark.unit
async def test_recovery_ancestry_checks_use_merge_safety_git_env(tmp_path: Path) -> None:
    """Ancestry and diff checks must ignore replace refs and graft overrides."""
    workspace_id = "ws_merge_safety_env"
    (tmp_path / workspace_id).mkdir()
    (tmp_path / workspace_id / ".git").write_text("gitdir: test\n", encoding="utf-8")
    remote_head = "a" * 40
    local_head = "b" * 40
    captured_envs: list[dict[str, str] | None] = []

    class _EnvCapturingRunner(_RollbackCommandRunner):
        async def run(self, args: list[str], **kwargs: object) -> CommandResult:
            captured_envs.append(kwargs.get("env"))
            return await super().run(args, **kwargs)

    await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        _runner(tmp_path, _EnvCapturingRunner(remote_head=remote_head, local_head=local_head)),
        workspace_id=workspace_id,
        worktree_path=tmp_path / workspace_id,
        remote_branch="fix/review",
        expected_remote_head=remote_head,
        local_head=local_head,
        state=MonitorState(),
    )

    merge_safety_envs = [
        env for env in captured_envs if env is not None and env.get("GIT_NO_REPLACE_OBJECTS") == "1"
    ]
    assert len(merge_safety_envs) >= 5
    assert all(env.get("GIT_GRAFT_FILE") == os.devnull for env in merge_safety_envs)


@pytest.mark.unit
async def test_recovery_reset_restores_real_tree_with_replace_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """refs/replace on FETCH_HEAD must not survive unpublished repair recovery.

    Regression for PRRT_kwDOSJAM6s6bebd_: without GIT_NO_REPLACE_OBJECTS,
    ``git reset --hard FETCH_HEAD`` checks out the replacement tree while HEAD
    still matches the fetched SHA, so verification falsely reports success.
    """
    monkeypatch.delenv("GIT_NO_REPLACE_OBJECTS", raising=False)
    monkeypatch.delenv("GIT_GRAFT_FILE", raising=False)
    monkeypatch.delenv("GIT_REPLACE_REF_BASE", raising=False)

    workspace_id = "ws_replace_recovery"
    worktree_path = tmp_path / workspace_id
    worktree_path.mkdir()
    _git(worktree_path, "init", "-q")
    _git(worktree_path, "config", "user.email", "awf@example.com")
    _git(worktree_path, "config", "user.name", "AWF Test")
    (worktree_path / "file.txt").write_text("remote\n", encoding="utf-8")
    _git(worktree_path, "add", "file.txt")
    _git(worktree_path, "commit", "-qm", "remote tip")
    remote_head = _git(worktree_path, "rev-parse", "HEAD").stdout.strip()

    (worktree_path / "file.txt").write_text("repair\n", encoding="utf-8")
    _git(worktree_path, "add", "file.txt")
    _git(worktree_path, "commit", "-qm", "unpublished repair")
    repair_head = _git(worktree_path, "rev-parse", "HEAD").stdout.strip()

    repair_tree = _git(worktree_path, "rev-parse", f"{repair_head}^{{tree}}").stdout.strip()
    forged = _git(
        worktree_path,
        "commit-tree",
        repair_tree,
        "-p",
        remote_head,
        "-m",
        "forged replacement",
    ).stdout.strip()
    _git(worktree_path, "update-ref", f"refs/replace/{remote_head}", forged)
    _write_fetch_head(worktree_path, remote_head)

    poisoned_reset = subprocess.run(
        ["git", "-C", str(worktree_path), "reset", "--hard", "FETCH_HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert poisoned_reset.returncode == 0
    assert (worktree_path / "file.txt").read_text(encoding="utf-8") == "repair\n"
    _git(worktree_path, "reset", "--hard", repair_head)

    async def _fetch_ok(**_kwargs: object) -> CommandResult:
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _deps=SimpleNamespace(runner=AsyncioSubprocessRunner()),
        _remote_branch_fetch_once=_fetch_ok,
    )

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        remote_branch="fix/review",
        expected_remote_head=remote_head,
        local_head=repair_head,
        state=MonitorState(),
    )

    assert result is None
    assert restored_head == remote_head
    assert (worktree_path / "file.txt").read_text(encoding="utf-8") == "remote\n"


@pytest.mark.unit
async def test_behind_remote_fast_forward_rejects_graft_forged_ancestry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grafts cannot fake behind-remote ancestry to reset before provenance checks.

    Regression for PRRT_kwDOSJAM6s6beOKI: ``behind.ok`` fast-forward reset must use
    the no-replace/no-graft merge-safety env so unrelated local commits are not
    destroyed when ``info/grafts`` forges parentage.
    """
    monkeypatch.delenv("GIT_NO_REPLACE_OBJECTS", raising=False)
    monkeypatch.delenv("GIT_GRAFT_FILE", raising=False)
    monkeypatch.delenv("GIT_REPLACE_REF_BASE", raising=False)

    workspace_id = "ws_graft_forgery"
    worktree_path = tmp_path / workspace_id
    repo, _ancestor, remote, lateral = _init_repo_with_lateral_and_remote(worktree_path)
    info_dir = repo / ".git" / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    (info_dir / "grafts").write_text(f"{remote} {lateral}\n", encoding="utf-8")
    _write_fetch_head(repo, remote)

    forged_check = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", lateral, remote],
        check=False,
        capture_output=True,
        text=True,
    )
    assert forged_check.returncode == 0

    async def _fetch_ok(**_kwargs: object) -> CommandResult:
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _deps=SimpleNamespace(runner=AsyncioSubprocessRunner()),
        _remote_branch_fetch_once=_fetch_ok,
    )

    restored_head, result = await remote_repair_unpublished._abandon_unpublished_comment_repairs(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        remote_branch="fix/review",
        expected_remote_head=remote,
        local_head=lateral,
        state=MonitorState(),
    )

    assert restored_head == lateral
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == lateral
