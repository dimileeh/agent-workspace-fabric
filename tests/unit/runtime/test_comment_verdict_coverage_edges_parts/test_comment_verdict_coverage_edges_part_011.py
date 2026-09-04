"""Protocol-retry rollback: restore trusted Git config before cleanup."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import AsyncioSubprocessRunner
from tests.unit.runtime.test_comment_verdict_coverage_edges_parts._helpers import (
    init_git_worktree,
    init_git_worktree_with_dirty_submodule,
)


def _init_linked_awf_worktree(tmp_path: Path, *, name: str = "ws_link") -> tuple[Path, Path, str]:
    """Create an AWF-shaped linked worktree under ``worktrees/`` + ``mirrors/``."""
    layout = tmp_path / "awf"
    worktrees = layout / "worktrees"
    mirrors = layout / "mirrors" / "repo.git"
    worktree = worktrees / name
    worktrees.mkdir(parents=True)

    src = tmp_path / "src_repo"
    src.mkdir()
    init_git_worktree(src)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=src,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "clone", "--bare", str(src), str(mirrors)],
        check=True,
        capture_output=True,
    )
    # The bare mirror does not inherit the source repo's identity; commits made
    # from the linked worktree must not depend on a global ``user.*`` (CI has none).
    for key, value in (("user.email", "awf@example.com"), ("user.name", "AWF Test")):
        subprocess.run(
            ["git", "-C", str(mirrors), "config", key, value],
            check=True,
            capture_output=True,
        )
    subprocess.run(
        ["git", "-C", str(mirrors), "worktree", "add", str(worktree), "HEAD"],
        check=True,
        capture_output=True,
    )
    linked = mirrors / "worktrees" / name
    assert (worktree / ".git").is_file()
    assert linked.is_dir()
    return worktree, linked, head


@pytest.mark.unit
@pytest.mark.asyncio
async def test_protocol_retry_rollback_restores_excludesfile_before_cleanup(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6e0yQG: restore trusted config before cleaning ignored residue.

    When a correction sets ``core.excludesFile`` to an agent-created file under
    ``.git`` and creates a matching untracked path, cleanup without ``-x`` leaves
    the path while the exclusion is active. Restoring the snapshot *after*
    cleanup would re-expose those bytes as untracked with no further check.
    """
    from awf.runtime.pr_monitor_runner import (
        comment_verdict,
        comment_verdict_residue_fingerprint,
    )

    worktree = tmp_path / "ws_excludesfile_order"
    worktree.mkdir()
    init_git_worktree(worktree)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert comment_verdict_residue_fingerprint.remember_item_start_local_git_configs(worktree)

    excludes = worktree / ".git" / "agent-excludes"
    excludes.write_text("poisoned-residue.txt\n", encoding="utf-8")
    subprocess.run(
        ["git", "config", "--local", "core.excludesFile", str(excludes)],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    residue = worktree / "poisoned-residue.txt"
    residue.write_text("rejected-correction-bytes\n", encoding="utf-8")

    # Under the poisoned excludesFile the path is ignored (not cleaned by -ffd).
    ignored = subprocess.run(
        ["git", "status", "--porcelain", "--ignored", "--", "poisoned-residue.txt"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "!!" in ignored or ignored.strip().startswith("!!")

    async def _rev_parse_head(_path: Path) -> str:
        return head

    runner = SimpleNamespace(
        _deps=SimpleNamespace(
            adapter=SimpleNamespace(is_hosted=False),
            runner=AsyncioSubprocessRunner(),
        ),
        _rev_parse_head=_rev_parse_head,
    )
    assert await comment_verdict._rollback_unaccepted_protocol_retry_changes(
        runner,
        workspace_id="ws_excludesfile_order",
        worktree_path=worktree,
        item_start_head=head,
        state=None,
    )

    get_excludes = subprocess.run(
        ["git", "config", "--local", "--get", "core.excludesFile"],
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    assert get_excludes.returncode != 0
    assert not residue.exists()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_item_start_git_config_snapshot_after_hooks_path_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6e0yQN: snapshot after hook repair so rollback cannot re-poison.

    Pre-launch repair clears a poisoned ``core.hooksPath``. If the item-start
    local-config snapshot is taken before that repair, non-FIXED rollback
    restores the executable hook path the safety repair just removed.
    """
    from awf.adapters.base import AgentRunResult
    from awf.runtime.pr_monitor_runner import comment_verdict

    worktree = tmp_path / "ws_hooks_snapshot_order"
    worktree.mkdir()
    init_git_worktree(worktree)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    poisoned_hooks = tmp_path / "poisoned-hooks"
    poisoned_hooks.mkdir()
    hook_script = poisoned_hooks / "pre-commit"
    hook_script.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook_script.chmod(0o755)
    subprocess.run(
        ["git", "config", "--local", "core.hooksPath", str(poisoned_hooks)],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    assert subprocess.run(
        ["git", "config", "--local", "--get", "core.hooksPath"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == str(poisoned_hooks)

    mirror_path = tmp_path / "mirror.git"
    mirror_path.mkdir()
    call_order: list[str] = []
    hooks_present_at_snapshot: list[bool] = []

    async def _ok_ownership(**_kwargs: object) -> bool:
        return True

    async def _repair_mirror_hooks_path(_path: Path) -> bool:
        call_order.append("repair")
        unset = subprocess.run(
            ["git", "config", "--local", "--unset-all", "core.hooksPath"],
            cwd=worktree,
            capture_output=True,
        )
        assert unset.returncode == 0
        return True

    real_remember = comment_verdict.remember_item_start_local_git_configs

    def _remember(path: Path) -> bool:
        call_order.append("remember")
        probe = subprocess.run(
            ["git", "config", "--local", "--get", "core.hooksPath"],
            cwd=path,
            capture_output=True,
            text=True,
        )
        hooks_present_at_snapshot.append(probe.returncode == 0)
        return real_remember(path)

    monkeypatch.setattr(comment_verdict, "repair_agent_runtime_ownership", _ok_ownership)
    monkeypatch.setattr(comment_verdict, "mirror_path_for_worktree", lambda _path: mirror_path)
    monkeypatch.setattr(comment_verdict, "repair_mirror_hooks_path", _repair_mirror_hooks_path)
    monkeypatch.setattr(comment_verdict, "remember_item_start_local_git_configs", _remember)

    async def _rev_parse_head(_path: Path) -> str:
        return head

    async def _run_agent(**_kwargs: object) -> AgentRunResult:
        # Re-poison after launch so rollback would restore poison if the
        # item-start snapshot captured the pre-repair baseline.
        subprocess.run(
            ["git", "config", "--local", "core.hooksPath", str(poisoned_hooks)],
            cwd=worktree,
            check=True,
            capture_output=True,
        )
        return AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: FALSE POSITIVE: pre-existing behavior is correct",
            stderr="",
        )

    async def _commit_dirty(**_kwargs: object) -> bool:
        return False

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _workspace_runtime_context="",
        _deps=SimpleNamespace(
            adapter=SimpleNamespace(is_hosted=False),
            runner=AsyncioSubprocessRunner(),
        ),
        _rev_parse_head=_rev_parse_head,
        _run_monitor_agent_with_service_recovery=_run_agent,
        _commit_dirty_worktree=_commit_dirty,
        _provider_recovery_suppresses_cli=lambda _ws: _async_false(),
        _resolve_task_tag=lambda _ws: _async_none(),
        _handle_provider_agent_run_error=_noop_provider_error,
    )

    result = await comment_verdict._invoke_cli_for_verdict_result(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_hooks_snapshot_order",
        prompt="review item",
        commit_message="fix: review item",
        compose_project="awf_ws_hooks",
        compose_file=tmp_path / "compose.yml",
        operation_start_head=head,
        require_fix_evidence=False,
        commit_dirty_changes=False,
    )

    assert call_order == ["repair", "remember"]
    assert hooks_present_at_snapshot == [False]
    assert result.verdict == "false_positive"

    get_hooks = subprocess.run(
        ["git", "config", "--local", "--get", "core.hooksPath"],
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    assert get_hooks.returncode != 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_item_start_git_config_snapshot_runs_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6e5nws: item-start config walk must not block the event loop.

    ``remember_item_start_local_git_configs`` recursively scans the worktree for
    nested ``.git`` markers (up to 100k entries / 30s). Running it inline in the
    async verdict path stalls unrelated workspace monitors on the same loop.
    """
    import asyncio

    from awf.adapters.base import AgentRunResult
    from awf.runtime.pr_monitor_runner import comment_verdict

    worktree = tmp_path / "ws_offloop_item_start_snapshot"
    worktree.mkdir()
    init_git_worktree(worktree)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    to_thread_funcs: list[str] = []
    original_to_thread = asyncio.to_thread

    async def _observe_to_thread(func: object, /, *args: object, **kwargs: object) -> object:
        name = getattr(func, "__name__", type(func).__name__)
        to_thread_funcs.append(str(name))
        return await original_to_thread(func, *args, **kwargs)  # type: ignore[arg-type]

    async def _ok_ownership(**_kwargs: object) -> bool:
        return True

    async def _rev_parse_head(_path: Path) -> str:
        return head

    async def _run_agent(**_kwargs: object) -> AgentRunResult:
        return AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: FALSE POSITIVE: pre-existing behavior is correct",
            stderr="",
        )

    async def _commit_dirty(**_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(comment_verdict.asyncio, "to_thread", _observe_to_thread)
    monkeypatch.setattr(comment_verdict, "repair_agent_runtime_ownership", _ok_ownership)
    monkeypatch.setattr(comment_verdict, "mirror_path_for_worktree", lambda _path: None)

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _workspace_runtime_context="",
        _deps=SimpleNamespace(
            adapter=SimpleNamespace(is_hosted=False),
            runner=AsyncioSubprocessRunner(),
        ),
        _rev_parse_head=_rev_parse_head,
        _run_monitor_agent_with_service_recovery=_run_agent,
        _commit_dirty_worktree=_commit_dirty,
        _provider_recovery_suppresses_cli=lambda _ws: _async_false(),
        _resolve_task_tag=lambda _ws: _async_none(),
        _handle_provider_agent_run_error=_noop_provider_error,
    )

    result = await comment_verdict._invoke_cli_for_verdict_result(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_offloop_item_start_snapshot",
        prompt="review item",
        commit_message="fix: review item",
        compose_project="awf_ws_offloop_snap",
        compose_file=tmp_path / "compose.yml",
        operation_start_head=head,
        require_fix_evidence=False,
        commit_dirty_changes=False,
    )

    assert result.verdict == "false_positive"
    assert "remember_item_start_local_git_configs" in to_thread_funcs


@pytest.mark.unit
@pytest.mark.asyncio
async def test_correction_git_metadata_snapshot_runs_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6e7pF6: correction git-meta walk must not block the event loop.

    ``_read_correction_pr_worthy_residue_fingerprint`` appends ``git-meta:`` via
    ``_snapshot_worktree_local_git_configs``, which can scan up to 100k entries.
    That snapshot must run through ``asyncio.to_thread`` like item-start remember().
    """
    import asyncio

    from awf.common.commands import CommandResult
    from awf.runtime.pr_monitor_runner import comment_verdict_residue

    worktree = tmp_path / "ws_offloop_correction_git_meta"
    worktree.mkdir()
    init_git_worktree(worktree)

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout="", stderr="", stdout_bytes=b"")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))

    to_thread_funcs: list[str] = []
    original_to_thread = asyncio.to_thread

    async def _observe_to_thread(func: object, /, *args: object, **kwargs: object) -> object:
        name = getattr(func, "__name__", type(func).__name__)
        to_thread_funcs.append(str(name))
        return await original_to_thread(func, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(comment_verdict_residue.asyncio, "to_thread", _observe_to_thread)

    fingerprint = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_offloop_correction_git_meta",
        worktree_path=worktree,
    )
    assert fingerprint is not None and fingerprint.startswith("git-meta:")
    assert "_snapshot_worktree_local_git_configs" in to_thread_funcs


async def _async_false() -> bool:
    return False


async def _async_none() -> None:
    return None


async def _noop_provider_error(*_args: object, **_kwargs: object) -> None:
    return None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_protocol_retry_rollback_restores_gitfile_linkage_and_pins_git_dir(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6e1Vy1: rollback must restore `.git` and pin to item-start git-dir.

    When a correction replaces the linked worktree gitfile with one pointing at
    another agent-controlled git-dir whose HEAD matches item_start_head, config
    restore alone rewrites the original paths while cleanup still runs through
    the replacement git-dir and can leave the workspace attached to attacker
    metadata.
    """
    from awf.runtime.pr_monitor_runner import (
        comment_verdict,
        comment_verdict_residue_fingerprint,
    )

    worktree, trusted_linked, head = _init_linked_awf_worktree(tmp_path)
    mirrors = trusted_linked.parent.parent
    original_gitfile = (worktree / ".git").read_text(encoding="utf-8")

    assert comment_verdict_residue_fingerprint.remember_item_start_local_git_configs(worktree)

    # Second linked worktree under the same mirror: complete metadata so cleanup
    # through the replacement git-dir can succeed while HEAD still matches.
    evil_checkout = worktree.parent / "ws_evil_checkout"
    subprocess.run(
        ["git", "-C", str(mirrors), "worktree", "add", str(evil_checkout), "HEAD"],
        check=True,
        capture_output=True,
    )
    evil = mirrors / "worktrees" / "ws_evil_checkout"
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(evil),
            "config",
            "--local",
            "core.excludesFile",
            "/tmp/attacker-excludes",
        ],
        check=True,
        capture_output=True,
    )
    (worktree / ".git").write_text(f"gitdir: {evil.resolve()}\n", encoding="utf-8")
    swapped_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert swapped_head.lower() == head.lower()

    observed_git_dirs: list[str] = []
    real_runner = AsyncioSubprocessRunner()

    async def _run(cmd: list[str], **kwargs: object) -> object:
        if cmd and cmd[0] == "git" and "--git-dir" in cmd:
            raw = cmd[cmd.index("--git-dir") + 1]
            # Resolve while the pin fd is still open (PRRT_kwDOSJAM6s6fIKd3).
            observed_git_dirs.append(str(Path(raw).resolve()))
        return await real_runner.run(cmd, **kwargs)  # type: ignore[arg-type]

    async def _rev_parse_head(_path: Path) -> str:
        return head

    runner = SimpleNamespace(
        _deps=SimpleNamespace(
            adapter=SimpleNamespace(is_hosted=False),
            runner=SimpleNamespace(run=_run),
        ),
        _rev_parse_head=_rev_parse_head,
    )
    assert await comment_verdict._rollback_unaccepted_protocol_retry_changes(
        runner,
        workspace_id="ws_gitfile_linkage",
        worktree_path=worktree,
        item_start_head=head,
        state=None,
    )

    restored_gitfile = (worktree / ".git").read_text(encoding="utf-8")
    assert restored_gitfile == original_gitfile
    assert any(Path(path) == trusted_linked.resolve() for path in observed_git_dirs)
    excludes = subprocess.run(
        ["git", "config", "--local", "--get", "core.excludesFile"],
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    assert excludes.returncode != 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_protocol_retry_rollback_pins_item_start_commondir(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fLlil: rollback must pin remembered common dir, not live commondir.

    Rewriting the linked worktree ``commondir`` to another accessible mirror lets
    ``git --git-dir <pinned-worktree-git-dir> reset --hard`` update branch refs in
    the foreign mirror when only the per-worktree directory is pinned.
    """
    from awf.runtime.pr_monitor_runner import (
        comment_verdict,
        comment_verdict_residue_fingerprint,
    )

    worktree, trusted_linked, head = _init_linked_awf_worktree(tmp_path)
    original_commondir = (trusted_linked / "commondir").read_text(encoding="utf-8")
    assert comment_verdict_residue_fingerprint.remember_item_start_local_git_configs(worktree)

    # Advance the worktree so rollback must run ``reset --hard``.
    (worktree / "advance.txt").write_text("advanced\n", encoding="utf-8")
    subprocess.run(["git", "add", "advance.txt"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "advance for rollback"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    advanced_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert advanced_head.lower() != head.lower()

    # Foreign bare mirror with the same tip as the advanced worktree.
    trusted_mirror = trusted_linked.parent.parent
    foreign = tmp_path / "awf" / "mirrors" / "foreign.git"
    subprocess.run(
        ["git", "clone", "--bare", str(trusted_mirror), str(foreign)],
        check=True,
        capture_output=True,
    )
    foreign_branch = subprocess.run(
        ["git", "-C", str(foreign), "symbolic-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(foreign), "update-ref", foreign_branch, advanced_head],
        check=True,
        capture_output=True,
    )
    foreign_tip_before = subprocess.run(
        ["git", "-C", str(foreign), "rev-parse", foreign_branch],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert foreign_tip_before.lower() == advanced_head.lower()

    (trusted_linked / "commondir").write_text(f"{foreign.resolve()}\n", encoding="utf-8")

    observed_common_dirs: list[str] = []
    real_runner = AsyncioSubprocessRunner()

    async def _run(cmd: list[str], **kwargs: object) -> object:
        env = kwargs.get("env")
        if isinstance(env, dict):
            common = env.get("GIT_COMMON_DIR")
            if isinstance(common, str):
                # Resolve while the pin fd is still open (PRRT_kwDOSJAM6s6fLlil).
                observed_common_dirs.append(str(Path(common).resolve()))
        return await real_runner.run(cmd, **kwargs)  # type: ignore[arg-type]

    async def _rev_parse_head(_path: Path) -> str:
        return advanced_head

    runner = SimpleNamespace(
        _deps=SimpleNamespace(
            adapter=SimpleNamespace(is_hosted=False),
            runner=SimpleNamespace(run=_run),
        ),
        _rev_parse_head=_rev_parse_head,
    )
    assert await comment_verdict._rollback_unaccepted_protocol_retry_changes(
        runner,
        workspace_id="ws_commondir_pin",
        worktree_path=worktree,
        item_start_head=head,
        state=None,
    )

    foreign_tip_after = subprocess.run(
        ["git", "-C", str(foreign), "rev-parse", foreign_branch],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert foreign_tip_after.lower() == foreign_tip_before.lower()
    restored_commondir = (trusted_linked / "commondir").read_text(encoding="utf-8")
    assert restored_commondir == original_commondir
    assert any(Path(path) == trusted_mirror.resolve() for path in observed_common_dirs)
    worktree_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert worktree_head.lower() == head.lower()


@pytest.mark.unit
def test_item_start_commondir_snapshot_restore_and_pin_helpers(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fLlil: remember/restore/pin linked ``commondir`` fail closed."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod
    from awf.runtime.pr_monitor_runner import (
        comment_verdict_residue_fingerprint_git_config as gc_mod,
    )

    worktree, linked, _head = _init_linked_awf_worktree(tmp_path, name="ws_cd")
    original = (linked / "commondir").read_text(encoding="utf-8")
    assert fp_mod.remember_item_start_local_git_configs(worktree)
    assert fp_mod.item_start_has_commondir(worktree)
    key = str(worktree.resolve())
    assert gc_mod._ITEM_START_COMMONDIR[key] == original.strip()

    (linked / "commondir").write_text("/tmp/attacker-common\n", encoding="utf-8")
    assert fp_mod.restore_item_start_local_git_configs(worktree)
    assert (linked / "commondir").read_text(encoding="utf-8") == (
        original if original.endswith("\n") else f"{original}\n"
    )

    with fp_mod.hold_item_start_pinned_common_dir(worktree) as pinned:
        assert pinned is not None
        assert pinned.resolve() == linked.parent.parent.resolve()

    # Non-regular ``commondir`` fails closed at snapshot time (avoid full remember:
    # a FIFO under the live linked git-dir can hang ignore probes).
    linkage = gc_mod._ITEM_START_GIT_LINKAGE[key]
    (linked / "commondir").unlink()
    os.mkfifo(linked / "commondir", mode=0o644)
    assert gc_mod._snapshot_linked_commondir_text(worktree, linkage) == (False, None)


@pytest.mark.unit
def test_item_start_git_linkage_snapshot_helpers_fail_closed(tmp_path: Path) -> None:
    """PRRT_kwDOSJAM6s6e1Vy1: gitfile snapshot helpers fail closed on bad markers."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    missing = tmp_path / "missing_ws"
    missing.mkdir()
    assert fp_mod._snapshot_outer_gitfile_text(missing) == (True, None)
    assert fp_mod.item_start_pinned_git_dir(missing) is None

    worktree = tmp_path / "ws_bad_gitfile"
    worktree.mkdir()
    (worktree / ".git").write_text("ref: refs/heads/main\n", encoding="utf-8")
    assert fp_mod._snapshot_outer_gitfile_text(worktree) == (False, None)
    assert fp_mod.remember_item_start_local_git_configs(worktree) is False

    linked = tmp_path / "linked_meta"
    linked.mkdir()
    assert fp_mod._resolve_gitfile_target(worktree, f"gitdir: {linked}\n") == linked.resolve()
    assert fp_mod._resolve_gitfile_target(worktree, "gitdir:\n") is None
    assert fp_mod._resolve_gitfile_target(worktree, "not-a-gitdir\n") is None
    rel = fp_mod._resolve_gitfile_target(worktree, "gitdir: ./rel-git\n")
    assert rel == (worktree / "rel-git").resolve()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_correction_residue_fingerprint_surfaces_self_ignored_newdir(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6e3D-C: self-ignored correction files must change the fingerprint.

    Creating ``newdir/.gitignore`` with ``*`` plus ``newdir/payload`` leaves
    ordinary ``git status --porcelain`` empty (no ``--ignored``). Both correction
    fingerprints look clean, mutation detection accepts non-FIXED, and rollback's
    ignored-path policy leaves the rejected bytes behind. Nested probes already
    omit ``--exclude-standard``; ordinary fingerprints must surface ignored
    entries via ``--ignored=matching`` independently of live ignore rules.
    """
    from awf.runtime.pr_monitor_runner import comment_verdict_residue
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_self_ignored"
    worktree.mkdir()
    init_git_worktree(worktree)

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=AsyncioSubprocessRunner()))

    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_self_ignored",
        worktree_path=worktree,
    )
    assert start_fp is not None
    assert start_fp.startswith("git-meta:")
    assert not fp_mod._fingerprint_has_pr_worthy_path_residue(start_fp)

    newdir = worktree / "newdir"
    newdir.mkdir()
    (newdir / ".gitignore").write_text("*\n", encoding="utf-8")
    (newdir / "payload").write_text("rejected-bytes\n", encoding="utf-8")

    # Confirm the hole ordinary porcelain without --ignored would leave open.
    plain = subprocess.run(
        ["git", "status", "--porcelain", "-z", "--untracked-files=all"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    assert plain.stdout == b""

    poisoned_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_self_ignored",
        worktree_path=worktree,
    )
    assert poisoned_fp is not None
    assert poisoned_fp != start_fp
    assert any(line.startswith("ignored:") for line in poisoned_fp.splitlines())
    assert not fp_mod._fingerprint_has_pr_worthy_path_residue(poisoned_fp)
    assert comment_verdict_residue._correction_authored_mutation_vs_start(
        attempt_start_head="abc123",
        pre_sink_head="abc123",
        correction_start_residue_fp=start_fp,
        pre_sink_residue_fp=poisoned_fp,
    )


@pytest.mark.unit
def test_ignored_residue_helpers_parse_and_hash_directory_entries(tmp_path: Path) -> None:
    """PRRT_kwDOSJAM6s6e3D-C / e4PhN: ignored helpers parse !! paths and digests dirs."""
    from awf.node.git_manager import git_env_without_object_lookup_overrides
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    assert fp_mod._ignored_paths_from_status_stdout(
        "!! newdir/.gitignore\n!! newdir/payload\n?? visible.py\n",
        is_z=False,
    ) == ["newdir/.gitignore", "newdir/payload"]
    assert fp_mod._ignored_paths_from_status_stdout(
        "!! newdir/.gitignore\0!! newdir/payload\0",
        is_z=True,
    ) == ["newdir/.gitignore", "newdir/payload"]

    worktree = tmp_path / "ws_ignored_dir"
    worktree.mkdir()
    init_git_worktree(worktree)
    vendor = worktree / "vendor"
    vendor.mkdir()
    (vendor / "cfg").write_text("a\n", encoding="utf-8")
    digest = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["vendor/"],
        git_env=git_env_without_object_lookup_overrides(),
    )
    assert digest is not None
    # Same tree content must be stable.
    repeat = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["vendor/"],
        git_env=git_env_without_object_lookup_overrides(),
    )
    assert digest == repeat
    (vendor / "cfg").write_text("b\n", encoding="utf-8")
    mutated = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["vendor/"],
        git_env=git_env_without_object_lookup_overrides(),
    )
    assert mutated is not None and mutated != digest
    empty = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=[],
        git_env=git_env_without_object_lookup_overrides(),
    )
    assert empty == hashlib.sha256().hexdigest()
    other_root = worktree / "other"
    other_root.mkdir()
    (other_root / "cfg").write_text("a\n", encoding="utf-8")
    other = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["other/"],
        git_env=git_env_without_object_lookup_overrides(),
    )
    assert other is not None and other != digest
    # Malformed root-only slash cannot be content-hashed; fail closed.
    assert (
        fp_mod._hash_ignored_residue_identity(
            worktree_path=worktree,
            ignored_paths=["/"],
            git_env=git_env_without_object_lookup_overrides(),
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_correction_residue_fingerprint_surfaces_ignored_dir_content_mutation(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6e4PhN: mutations under a pre-existing ignored dir must fingerprint.

    When ``vendor/`` already exists at correction start, Git reports only
    ``!! vendor/`` before and after edits beneath it. Path-only ignored-dir
    identity would collide; rollback then leaves the mutated ignored bytes.
    """
    from awf.runtime.pr_monitor_runner import comment_verdict_residue

    worktree = tmp_path / "ws_ignored_dir_mutate"
    worktree.mkdir()
    init_git_worktree(worktree)
    (worktree / ".gitignore").write_text("vendor/\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", ".gitignore"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "ignore vendor"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    vendor = worktree / "vendor"
    vendor.mkdir()
    (vendor / "cfg").write_text("baseline\n", encoding="utf-8")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=AsyncioSubprocessRunner()))
    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_ignored_dir_mutate",
        worktree_path=worktree,
    )
    assert start_fp is not None
    assert any(line.startswith("ignored:") for line in start_fp.splitlines())

    before_status = subprocess.run(
        [
            "git",
            "status",
            "--porcelain",
            "--ignored=matching",
            "--untracked-files=all",
        ],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "!! vendor/" in before_status

    (vendor / "cfg").write_text("poisoned\n", encoding="utf-8")

    after_status = subprocess.run(
        [
            "git",
            "status",
            "--porcelain",
            "--ignored=matching",
            "--untracked-files=all",
        ],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert after_status == before_status

    poisoned_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_ignored_dir_mutate",
        worktree_path=worktree,
    )
    assert poisoned_fp is not None
    assert poisoned_fp != start_fp
    assert any(line.startswith("ignored:") for line in poisoned_fp.splitlines())
    assert comment_verdict_residue._correction_authored_mutation_vs_start(
        attempt_start_head="abc123",
        pre_sink_head="abc123",
        correction_start_residue_fp=start_fp,
        pre_sink_residue_fp=poisoned_fp,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_correction_residue_fingerprint_combines_pr_worthy_and_ignored(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6e3D-C: ignored identity attaches alongside PR-worthy residue."""
    from awf.common.commands import CommandResult
    from awf.runtime.pr_monitor_runner import comment_verdict_residue
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_combo_ignored"
    worktree.mkdir()
    init_git_worktree(worktree)
    (worktree / "src" / "new.py").write_text("visible\n", encoding="utf-8")
    newdir = worktree / "newdir"
    newdir.mkdir()
    (newdir / ".gitignore").write_text("*\n", encoding="utf-8")
    (newdir / "payload").write_text("hidden\n", encoding="utf-8")

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            stdout = "?? src/new.py\n!! newdir/.gitignore\n!! newdir/payload\n"
            return CommandResult(returncode=0, stdout=stdout, stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))
    fingerprint = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_combo_ignored",
        worktree_path=worktree,
    )
    assert fingerprint is not None
    assert any(line.startswith("ignored:") for line in fingerprint.splitlines())
    assert fp_mod._fingerprint_has_pr_worthy_path_residue(fingerprint)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_correction_start_head_probe_avoids_live_include_path_fifo(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6e30Rp: correction-start HEAD must not hang on include.path FIFO.

    After item-start config is snapshotted, attempt 0 can inject ``include.path``
    pointing at a reader-less FIFO. Live ``git rev-parse HEAD`` blocks on Git
    2.43; the attempt-start probe must use remembered configs and a timeout.
    """

    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_fifo_include_head"
    worktree.mkdir()
    init_git_worktree(worktree)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert fp_mod.remember_item_start_local_git_configs(worktree) is True
    assert fp_mod.item_start_has_local_git_config_snapshot(worktree) is True

    fifo = tmp_path / "poison.fifo"
    os.mkfifo(fifo, mode=0o644)
    subprocess.run(
        ["git", "config", "include.path", str(fifo)],
        cwd=worktree,
        check=True,
        capture_output=True,
    )

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=AsyncioSubprocessRunner()))
    parsed = await fp_mod.read_protocol_attempt_start_head(
        runner,
        worktree_path=worktree,
        rev_parse_head=None,
    )
    assert parsed == head


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_correction_start_head_probe_avoids_fifo_on_linked_worktree(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6e30Rp: linked worktree HEAD probe also uses snapshotted configs."""

    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree, _linked, head = _init_linked_awf_worktree(tmp_path, name="ws_fifo_linked")
    assert fp_mod.remember_item_start_local_git_configs(worktree) is True

    fifo = tmp_path / "poison_linked.fifo"
    os.mkfifo(fifo, mode=0o644)
    subprocess.run(
        ["git", "config", "include.path", str(fifo)],
        cwd=worktree,
        check=True,
        capture_output=True,
    )

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=AsyncioSubprocessRunner()))
    parsed = await fp_mod.read_protocol_attempt_start_head(
        runner,
        worktree_path=worktree,
        rev_parse_head=None,
    )
    assert parsed == head


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_trusted_head_helper_covers_post_agent_fifo_probe_contract(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6e4egQ: shared trusted helper must survive include.path FIFO.

    Pre-sink and correction-end now call ``read_protocol_attempt_start_head``.
    Keep a direct FIFO regression on that helper so a live-config hang cannot
    regress under the post-agent probe contract.
    """

    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_fifo_post_agent_head"
    worktree.mkdir()
    init_git_worktree(worktree)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert fp_mod.remember_item_start_local_git_configs(worktree) is True

    fifo = tmp_path / "poison_post_agent.fifo"
    os.mkfifo(fifo, mode=0o644)
    subprocess.run(
        ["git", "config", "include.path", str(fifo)],
        cwd=worktree,
        check=True,
        capture_output=True,
    )

    async def _live_rev_parse(_path: Path) -> str | None:
        raise AssertionError("covered snapshot must not fall back to live rev-parse")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=AsyncioSubprocessRunner()))
    parsed = await fp_mod.read_protocol_attempt_start_head(
        runner,
        worktree_path=worktree,
        rev_parse_head=_live_rev_parse,
    )
    assert parsed == head


@pytest.mark.unit
@pytest.mark.asyncio
async def test_correction_residue_fingerprint_includes_submodule_local_git_config(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6e4egX: submodule config-only poison must change git-meta.

    Parent porcelain stays clean when only ``sub/.git`` (modules) local config
    mutates; outer-only snapshots collided and rollback left nested rewrites.
    """
    from types import SimpleNamespace

    from awf.common.commands import CommandResult
    from awf.runtime.pr_monitor_runner import comment_verdict_residue
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_submodule_git_meta"
    worktree.mkdir()
    init_git_worktree_with_dirty_submodule(worktree)
    # Stabilize submodule HEAD so parent status is clean aside from our config.
    subprocess.run(
        ["git", "add", "sub"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "pin sub"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )

    assert fp_mod.remember_item_start_local_git_configs(worktree) is True

    async def _run(cmd: list[str], **_kwargs: object) -> CommandResult:
        if "status" in cmd:
            return CommandResult(returncode=0, stdout="", stderr="", stdout_bytes=b"")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=SimpleNamespace(run=_run)))
    start_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_submodule_git_meta",
        worktree_path=worktree,
    )
    assert start_fp is not None
    assert start_fp.startswith("git-meta:")

    poison_key = "url.file:///attacker/.insteadOf"
    poison_value = "https://github.com/"
    subprocess.run(
        ["git", "config", "--local", poison_key, poison_value],
        cwd=worktree / "sub",
        check=True,
        capture_output=True,
    )
    parent_status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert parent_status == ""

    poisoned_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_submodule_git_meta",
        worktree_path=worktree,
    )
    assert poisoned_fp is not None
    assert poisoned_fp != start_fp

    assert fp_mod.restore_item_start_local_git_configs(worktree) is True
    restored = subprocess.run(
        ["git", "config", "--local", "--get", poison_key],
        cwd=worktree / "sub",
        capture_output=True,
        text=True,
    )
    assert restored.returncode != 0
    restored_fp = await comment_verdict_residue._read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id="ws_submodule_git_meta",
        worktree_path=worktree,
    )
    assert restored_fp == start_fp
