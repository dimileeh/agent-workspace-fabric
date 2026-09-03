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
    init_git_worktree_with_embedded_repo,
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
    from awf.runtime.pr_monitor_runner import comment_verdict, comment_verdict_rollback

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
    monkeypatch.setattr(
        comment_verdict_rollback,
        "repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
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
            observed_git_dirs.append(cmd[cmd.index("--git-dir") + 1])
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
    assert any(Path(path).resolve() == trusted_linked.resolve() for path in observed_git_dirs)
    excludes = subprocess.run(
        ["git", "config", "--local", "--get", "core.excludesFile"],
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    assert excludes.returncode != 0


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


@pytest.mark.unit
def test_module_git_dirs_under_and_nested_worktree_roots_helpers(tmp_path: Path) -> None:
    """PRRT_kwDOSJAM6s6e4egX: module walk + nested `.git` marker discovery."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_nested_helpers"
    worktree.mkdir()
    init_git_worktree_with_dirty_submodule(worktree)
    # This Git layout may keep ``sub/.git`` as a directory (no ``modules/``);
    # nested-marker discovery must still see the checkout.
    found_sub = fp_mod._nested_worktree_roots_with_git_markers(worktree)
    assert found_sub is not None
    assert any(path.name == "sub" for path in found_sub)

    # Synthetic ``modules/<name>`` tree under the outer git-dir.
    outer_git = (worktree / ".git").resolve()
    module_git = outer_git / "modules" / "synth"
    module_git.mkdir(parents=True)
    (module_git / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    modules = fp_mod._module_git_dirs_under(outer_git, roots=(worktree.resolve(),))
    assert modules is not None
    assert any(path.name == "synth" for path in modules)

    worktree2 = tmp_path / "ws_nested_helpers2"
    worktree2.mkdir()
    nested_name = init_git_worktree_with_embedded_repo(worktree2, nested_name="vendor_nested")
    found = fp_mod._nested_worktree_roots_with_git_markers(worktree2)
    assert found is not None
    assert any(path.name == nested_name for path in found)

    # Symlinked modules/ must fail closed.
    worktree3 = tmp_path / "ws_modules_symlink"
    worktree3.mkdir()
    init_git_worktree(worktree3)
    git_dir = (worktree3 / ".git").resolve()
    (git_dir / "modules").mkdir()
    (git_dir / "modules").rmdir()
    (git_dir / "modules").symlink_to(tmp_path / "elsewhere")
    assert fp_mod._module_git_dirs_under(git_dir, roots=(worktree3.resolve(),)) is None


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_protocol_retry_rollback_initial_head_avoids_fifo_via_trusted_reader(
    tmp_path: Path,
) -> None:
    """Review 5101264783: rollback pre-restore HEAD must use remembered configs.

    Attempt 0 can inject ``include.path`` → FIFO before non-FIXED rollback. The
    initial HEAD probe runs before Git configuration restore; a live
    ``_rev_parse_head`` would hang. Route through the trusted reader instead.
    """
    from awf.runtime.pr_monitor_runner import (
        comment_verdict,
        comment_verdict_residue_fingerprint,
    )

    worktree = tmp_path / "ws_rollback_fifo_head"
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

    fifo = tmp_path / "rollback_poison.fifo"
    os.mkfifo(fifo, mode=0o644)
    subprocess.run(
        ["git", "config", "include.path", str(fifo)],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    (worktree / "agent-edit.txt").write_text("scratch\n", encoding="utf-8")

    async def _live_rev_parse(_path: Path, **_kwargs: object) -> str | None:
        raise AssertionError("covered snapshot must not fall back to live rev-parse")

    runner = SimpleNamespace(
        _deps=SimpleNamespace(
            adapter=SimpleNamespace(is_hosted=False),
            runner=AsyncioSubprocessRunner(),
        ),
        _rev_parse_head=_live_rev_parse,
    )
    assert await comment_verdict._rollback_unaccepted_protocol_retry_changes(
        runner,
        workspace_id="ws_rollback_fifo_head",
        worktree_path=worktree,
        item_start_head=head,
        state=None,
    )
    assert not (worktree / "agent-edit.txt").exists()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_protocol_retry_rollback_initial_head_fallback_passes_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review 5101264783: no-snapshot rollback HEAD fallback must be finite."""
    from awf.common.commands import CommandResult
    from awf.runtime.pr_monitor_runner import comment_verdict, comment_verdict_residue
    from awf.runtime.validation_worktree import (
        ValidationWorktreeCheck,
        ValidationWorktreeCleanup,
    )

    worktree = tmp_path / "ws_rollback_timeout_fallback"
    worktree.mkdir()
    start = "a" * 40
    captured: dict[str, object] = {}

    async def _cleanup(**_kwargs: object) -> ValidationWorktreeCleanup:
        return ValidationWorktreeCleanup(
            cleaned=True,
            check=ValidationWorktreeCheck(clean=True, paths=()),
            restore_ref=start,
        )

    monkeypatch.setattr(
        "awf.runtime.validation_worktree.cleanup_validation_worktree_side_effects",
        _cleanup,
    )

    async def _rev_parse_head(_path: Path, *, timeout_seconds: float | None = None) -> str:
        captured["timeout_seconds"] = timeout_seconds
        return start

    async def _run(cmd: list[str], **kwargs: object) -> CommandResult:
        del cmd
        captured.setdefault("run_timeouts", []).append(kwargs.get("timeout_seconds"))
        return CommandResult(returncode=0, stdout=f"{start}\n", stderr="")

    runner = SimpleNamespace(
        _deps=SimpleNamespace(
            adapter=SimpleNamespace(is_hosted=False),
            runner=SimpleNamespace(run=_run),
        ),
        _rev_parse_head=_rev_parse_head,
    )
    assert await comment_verdict._rollback_unaccepted_protocol_retry_changes(
        runner,
        workspace_id="ws_rollback_timeout_fallback",
        worktree_path=worktree,
        item_start_head=start,
        state=None,
    )
    assert (
        captured["timeout_seconds"] == comment_verdict_residue._RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS
    )
    assert comment_verdict_residue._RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS in (
        captured.get("run_timeouts") or []
    )


@pytest.mark.unit
def test_ignored_dir_hash_falls_back_to_metadata_when_content_budget_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6e4fPN: oversized ignored dirs must not yield None identity.

    Content hashing reuses the 32 MiB worktree budget; typical ignored roots
    exceed it. Failing closed treats a stable large tree as mutation and rejects
    clean non-FIXED corrections. Metadata identity must still differ on size change.
    """
    from awf.node.git_manager import git_env_without_object_lookup_overrides
    from awf.runtime.pr_monitor_runner import comment_verdict_residue as residue
    from awf.runtime.pr_monitor_runner import comment_verdict_residue_fingerprint as fp_mod

    worktree = tmp_path / "ws_ignored_budget"
    worktree.mkdir()
    init_git_worktree(worktree)
    vendor = worktree / "vendor"
    vendor.mkdir()
    (vendor / "a").write_text("one\n", encoding="utf-8")
    (vendor / "b").write_text("two\n", encoding="utf-8")

    monkeypatch.setattr(
        residue,
        "_hash_worktree_directory_residue",
        lambda **_kwargs: None,
    )
    git_env = git_env_without_object_lookup_overrides()
    baseline = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["vendor/"],
        git_env=git_env,
    )
    assert baseline is not None
    repeat = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["vendor/"],
        git_env=git_env,
    )
    assert repeat == baseline
    (vendor / "a").write_text("one-mutated-longer\n", encoding="utf-8")
    mutated = fp_mod._hash_ignored_residue_identity(
        worktree_path=worktree,
        ignored_paths=["vendor/"],
        git_env=git_env,
    )
    assert mutated is not None and mutated != baseline
