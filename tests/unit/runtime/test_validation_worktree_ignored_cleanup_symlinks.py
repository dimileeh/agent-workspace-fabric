"""Symlink-baseline and core.symlinks validation-worktree cleanup regressions."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

import awf.runtime.validation_worktree_probes as validation_worktree_probes
from awf.common.commands import AsyncioSubprocessRunner, CommandResult
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
    VALIDATION_WORKTREE_STATUS_FAILED,
    _core_symlinks_enabled,
    _index_symlink_paths,
    _placeholder_baseline_rematerialized_symlink_paths,
    _run_validation_git,
    _unlink_worktree_symlink_nofollow,
    _worktree_entry_is_symlink_nofollow,
    check_validation_worktree_clean,
    cleanup_validation_worktree_side_effects,
    read_validation_worktree_symlink_form_baseline,
)
from tests.unit.runtime.test_validation_worktree_ignored_cleanup import (
    _init_real_worktree,
    _real_run_git,
    _run_real_git,
)


@pytest.mark.unit
async def test_index_symlink_paths_parses_nul_delimited_special_characters() -> None:
    """PRRT_kwDOSJAM6s6e9Z28: ``ls-files -s -z`` must preserve verbatim symlink paths."""
    stdout = "100644 deadbeef 0\tplain.py\0" + "120000 cafebabe 0\tlink\tname\0"

    async def run_git(args: list[str]) -> CommandResult:
        assert args == ["ls-files", "-s", "-z"]
        return CommandResult(returncode=0, stdout=stdout, stderr="")

    paths = await _index_symlink_paths(run_git)
    assert paths == ("link\tname",)


@pytest.mark.unit
async def test_index_symlink_paths_prefers_stdout_bytes_for_invalid_utf8() -> None:
    """PRRT_kwDOSJAM6s6fBSSD: decode ``ls-files -z`` via stdout_bytes + surrogateescape.

    Replacement-decoded stdout turns invalid path bytes into ``�``, which does
    not exist on disk, so the symlink-form baseline would incorrectly become
    False and omit protective ``-c core.symlinks=true``.
    """
    raw_path = b"link-\xff-name"
    payload = b"120000 cafebabe 0\t" + raw_path + b"\0"
    replace_stdout = payload.decode("utf-8", errors="replace")
    expected = raw_path.decode("utf-8", errors="surrogateescape")
    assert "\ufffd" in replace_stdout
    assert expected.encode("utf-8", errors="surrogateescape") == raw_path

    async def run_git(args: list[str]) -> CommandResult:
        assert args == ["ls-files", "-s", "-z"]
        return CommandResult(
            returncode=0,
            stdout=replace_stdout,
            stderr="",
            stdout_bytes=payload,
        )

    paths = await _index_symlink_paths(run_git)
    assert paths == (expected,)
    assert paths[0].encode("utf-8", errors="surrogateescape") == raw_path


@pytest.mark.unit
async def test_index_symlink_paths_failure_is_indeterminate() -> None:
    """PRRT_kwDOSJAM6s6fBSSK: ``ls-files`` failure must not look like an empty index."""

    async def run_git(args: list[str]) -> CommandResult:
        assert args == ["ls-files", "-s", "-z"]
        return CommandResult(
            returncode=124,
            stdout="",
            stderr="timed out",
            reason_code="COMMAND_TIMEOUT",
        )

    assert await _index_symlink_paths(run_git) is None


@pytest.mark.unit
async def test_symlink_form_baseline_indeterminate_when_ls_files_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6fBSSK: listing failure must not record FS capability True.

    Treating a failed ``ls-files`` as an empty index on a symlink-capable
    filesystem persists True, so later validation forces ``core.symlinks=true``
    against a legitimate placeholder checkout and can block or mutate it.
    """
    worktree = tmp_path / "worktree"
    _init_real_worktree(worktree, gitignore="")
    capability_calls: list[Path] = []

    def _capability(path: Path) -> bool:
        capability_calls.append(path)
        return True

    monkeypatch.setattr(
        "awf.runtime.validation_worktree_probes._worktree_filesystem_supports_symlinks",
        _capability,
    )

    async def run_git(args: list[str]) -> CommandResult:
        assert args == ["ls-files", "-s", "-z"]
        return CommandResult(returncode=1, stdout="", stderr="index locked")

    baseline = await read_validation_worktree_symlink_form_baseline(run_git, worktree)
    assert baseline is None
    assert capability_calls == []


@pytest.mark.unit
async def test_symlink_form_baseline_mixed_forms_fail_closed(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fK4k2 / PRRT_kwDOSJAM6s6fIJuG: mixed forms fail closed.

    Under ``core.symlinks=false``, a checkout can have some index symlinks still
    as real links and others as placeholders. Collapsing mixed → ``True`` forces
    tracking and permanently dirties placeholders (PRRT_kwDOSJAM6s6fIJuG).
    Collapsing mixed → ``False`` disables forced tracking for every path, so
    replacing the remaining real symlink with an equal-target regular file
    leaves ``git status --porcelain`` empty (PRRT_kwDOSJAM6s6fK4k2). Git's
    ``core.symlinks`` is global, so mixed forms must return ``None`` and fail
    closed.
    """
    worktree = tmp_path / "worktree"
    _init_real_worktree(worktree, gitignore="")
    _run_real_git(worktree, "config", "core.symlinks", "true")
    link_a = worktree / "link-a"
    link_b = worktree / "link-b"
    link_a.symlink_to("target-a")
    link_b.symlink_to("target-b")
    _run_real_git(worktree, "add", "link-a", "link-b")
    _run_real_git(
        worktree,
        "-c",
        "user.email=awf@example.test",
        "-c",
        "user.name=AWF Test",
        "commit",
        "-m",
        "add links",
    )
    _run_real_git(worktree, "config", "core.symlinks", "false")
    # Materialize only one path as a placeholder; leave the other as a symlink.
    link_b.unlink()
    link_b.write_bytes(b"target-b")
    assert link_a.is_symlink()
    assert not link_b.is_symlink()

    baseline = await read_validation_worktree_symlink_form_baseline(
        _real_run_git(worktree),
        worktree,
    )
    assert baseline is None

    # Equal-target rematerialization of the remaining real symlink stays hidden
    # under core.symlinks=false, but is visible once tracking is forced.
    link_a.unlink()
    link_a.write_bytes(b"target-a")
    assert not link_a.is_symlink()
    hidden = _run_real_git(
        worktree,
        "status",
        "--porcelain",
        "--untracked-files=all",
    )
    assert hidden.stdout.strip() == ""
    forced = _run_real_git(
        worktree,
        "-c",
        "core.symlinks=true",
        "status",
        "--porcelain",
        "--untracked-files=all",
    )
    assert "link-a" in forced.stdout
    assert "link-b" in forced.stdout

    check = await check_validation_worktree_clean(
        run_git=_real_run_git(worktree),
        worktree_path=worktree,
        ignore_all_ignored=True,
        trusted_index_symlinks_are_symlinks=baseline,
    )
    assert check.clean is False


@pytest.mark.unit
async def test_cleanup_restores_symlink_when_core_symlinks_false(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6ezrHU: cleanup must restore symlink when core.symlinks=false.

    Without ``-c core.symlinks=true``, status omits the typechange and
    ``git restore`` / ``reset --hard`` leave the regular file behind.
    """
    worktree = tmp_path / "worktree"
    restore_ref = _init_real_worktree(worktree, gitignore="")
    _run_real_git(worktree, "config", "core.symlinks", "true")
    link = worktree / "link"
    link.symlink_to("target")
    _run_real_git(worktree, "add", "link")
    _run_real_git(
        worktree,
        "-c",
        "user.email=awf@example.test",
        "-c",
        "user.name=AWF Test",
        "commit",
        "-m",
        "add link",
    )
    restore_ref = _run_real_git(worktree, "rev-parse", "HEAD").stdout.strip()
    _run_real_git(worktree, "config", "core.symlinks", "false")
    link.unlink()
    link.write_bytes(b"target")

    poisoned = _run_real_git(worktree, "status", "--porcelain", "--untracked-files=all")
    assert poisoned.stdout.strip() == ""

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=_real_run_git(worktree),
        worktree_path=worktree,
        restore_ref=restore_ref,
        trusted_index_symlinks_are_symlinks=True,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert link.is_symlink()
    assert link.readlink() == Path("target")


@pytest.mark.unit
async def test_empty_symlink_baseline_preserves_capable_checkout(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6e-Zcu / PRRT_kwDOSJAM6s6fA_x2: empty capable FS → True.

    A symlink-capable worktree with no index symlinks yet must not be recorded
    as a placeholder checkout. Otherwise an agent can add a symlink, flip
    ``core.symlinks=false``, replace the link with a plain file, and bypass
    protective ``-c core.symlinks=true`` because the baseline was False.
    Persist filesystem capability, not shared agent-writable Git config.
    """
    worktree = tmp_path / "worktree"
    restore_ref = _init_real_worktree(worktree, gitignore="")
    _run_real_git(worktree, "config", "core.symlinks", "true")

    pre_agent_baseline = await read_validation_worktree_symlink_form_baseline(
        _real_run_git(worktree),
        worktree,
    )
    assert pre_agent_baseline is True

    link = worktree / "link"
    link.symlink_to("target")
    _run_real_git(worktree, "add", "link")
    _run_real_git(
        worktree,
        "-c",
        "user.email=awf@example.test",
        "-c",
        "user.name=AWF Test",
        "commit",
        "-m",
        "add link",
    )
    restore_ref = _run_real_git(worktree, "rev-parse", "HEAD").stdout.strip()

    _run_real_git(worktree, "config", "core.symlinks", "false")
    link.unlink()
    link.write_bytes(b"target")

    poisoned = _run_real_git(worktree, "status", "--porcelain", "--untracked-files=all")
    assert poisoned.stdout.strip() == ""

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=_real_run_git(worktree),
        worktree_path=worktree,
        restore_ref=restore_ref,
        trusted_index_symlinks_are_symlinks=pre_agent_baseline,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert link.is_symlink()
    assert link.readlink() == Path("target")


@pytest.mark.unit
async def test_empty_symlink_baseline_ignores_shared_core_symlinks_false(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fA_x2: empty baseline must ignore shared core.symlinks=false.

    Linked worktrees share bare-mirror config. Trusting ``core.symlinks`` when
    the index is empty lets a sibling (or prior) agent poison the baseline to
    False so a later symlink→file rematerialization passes clean checks.
    Capable filesystems must persist True and keep forced symlink tracking.
    """
    worktree = tmp_path / "worktree"
    restore_ref = _init_real_worktree(worktree, gitignore="")
    _run_real_git(worktree, "config", "core.symlinks", "false")

    pre_agent_baseline = await read_validation_worktree_symlink_form_baseline(
        _real_run_git(worktree),
        worktree,
    )
    assert pre_agent_baseline is True

    link = worktree / "link"
    link.symlink_to("target")
    _run_real_git(worktree, "add", "link")
    _run_real_git(
        worktree,
        "-c",
        "user.email=awf@example.test",
        "-c",
        "user.name=AWF Test",
        "commit",
        "-m",
        "add link",
    )
    restore_ref = _run_real_git(worktree, "rev-parse", "HEAD").stdout.strip()
    # Rematerialize under core.symlinks=false (plain-file placeholder).
    link.unlink()
    _run_real_git(worktree, "checkout", "HEAD", "--", "link")
    assert link.exists()
    assert not link.is_symlink()

    forced = _run_real_git(
        worktree,
        "-c",
        "core.symlinks=true",
        "status",
        "--porcelain",
        "--untracked-files=all",
    )
    assert "link" in forced.stdout

    poisoned = _run_real_git(worktree, "status", "--porcelain", "--untracked-files=all")
    assert poisoned.stdout.strip() == ""

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=_real_run_git(worktree),
        worktree_path=worktree,
        restore_ref=restore_ref,
        trusted_index_symlinks_are_symlinks=pre_agent_baseline,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert link.is_symlink()
    assert link.readlink() == Path("target")


@pytest.mark.unit
async def test_empty_symlink_baseline_ignores_sibling_worktree_config_poison(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fA_x2: sibling worktree core.symlinks must not poison baseline."""
    bare = tmp_path / "repo.git"
    seed = tmp_path / "seed"
    worktree_a = tmp_path / "A"
    worktree_b = tmp_path / "B"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    subprocess.run(
        ["git", "clone", str(bare), str(seed)],
        check=True,
        capture_output=True,
        text=True,
    )
    (seed / "f").write_text("hi\n", encoding="utf-8")
    _run_real_git(seed, "add", "f")
    _run_real_git(
        seed,
        "-c",
        "user.email=awf@example.test",
        "-c",
        "user.name=AWF Test",
        "commit",
        "-m",
        "init",
    )
    _run_real_git(seed, "push", "origin", "HEAD:main")
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(bare),
            "worktree",
            "add",
            "-b",
            "branch-a",
            str(worktree_a),
            "main",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(bare),
            "worktree",
            "add",
            "-b",
            "branch-b",
            str(worktree_b),
            "main",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    _run_real_git(worktree_a, "config", "core.symlinks", "false")
    assert _run_real_git(
        worktree_b, "config", "--bool", "--get", "core.symlinks"
    ).stdout.strip() == ("false")

    baseline_b = await read_validation_worktree_symlink_form_baseline(
        _real_run_git(worktree_b),
        worktree_b,
    )
    assert baseline_b is True


@pytest.mark.unit
async def test_empty_symlink_baseline_false_when_filesystem_rejects_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty index on a non-symlink filesystem must persist False."""
    worktree = tmp_path / "worktree"
    _init_real_worktree(worktree, gitignore="")
    monkeypatch.setattr(
        "awf.runtime.validation_worktree_probes._worktree_filesystem_supports_symlinks",
        lambda _path: False,
    )

    baseline = await read_validation_worktree_symlink_form_baseline(
        _real_run_git(worktree),
        worktree,
    )
    assert baseline is False


@pytest.mark.unit
def test_worktree_filesystem_supports_symlinks_probe_and_cleanup(tmp_path: Path) -> None:
    """Capability probe must create a real symlink and leave no residue."""
    from awf.runtime.validation_worktree import _worktree_filesystem_supports_symlinks

    assert _worktree_filesystem_supports_symlinks(tmp_path) is True
    leftovers = list(tmp_path.glob(".awf-symlink-cap-*"))
    assert leftovers == []


@pytest.mark.unit
def test_worktree_filesystem_supports_symlinks_false_when_create_not_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful create that is not a real symlink is demonstrated False."""
    from awf.runtime.validation_worktree import _worktree_filesystem_supports_symlinks

    def _create_regular(self: Path, _target: str, *_args: object, **_kwargs: object) -> None:
        self.write_text("awf-symlink-cap-target", encoding="utf-8")

    monkeypatch.setattr(Path, "symlink_to", _create_regular)
    assert _worktree_filesystem_supports_symlinks(tmp_path) is False
    leftovers = list(tmp_path.glob(".awf-symlink-cap-*"))
    assert leftovers == []


@pytest.mark.unit
def test_worktree_filesystem_supports_symlinks_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6fGb8R: create OSError is indeterminate, not False.

    Treating operational probe failures (temporarily unwritable worktree, etc.)
    as proven lack of symlink support persists False and lets an agent later
    bypass forced type-change checks after permissions are restored.
    """
    from awf.runtime.validation_worktree import _worktree_filesystem_supports_symlinks

    def _raise_oserror(self: Path, _target: str, *_args: object, **_kwargs: object) -> None:
        raise OSError("symlinks disabled")

    monkeypatch.setattr(Path, "symlink_to", _raise_oserror)
    assert _worktree_filesystem_supports_symlinks(tmp_path) is None


@pytest.mark.unit
async def test_empty_symlink_baseline_indeterminate_when_capability_probe_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty-index baseline must not persist False when the FS probe errors."""
    worktree = tmp_path / "worktree"
    _init_real_worktree(worktree, gitignore="")

    def _raise_oserror(self: Path, _target: str, *_args: object, **_kwargs: object) -> None:
        raise OSError("worktree temporarily unwritable")

    monkeypatch.setattr(Path, "symlink_to", _raise_oserror)

    baseline = await read_validation_worktree_symlink_form_baseline(
        _real_run_git(worktree),
        worktree,
    )
    assert baseline is None


@pytest.mark.unit
def test_worktree_filesystem_supports_symlinks_fails_closed_on_unlink_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlink failure after create must not report success with residue left behind.

    Returning True while ``.awf-symlink-cap-*`` remains lets a later
    ``git add -A`` stage an AWF probe into the user PR (PRRT_kwDOSJAM6s6fBSST).
    """
    from awf.runtime.validation_worktree import _worktree_filesystem_supports_symlinks

    real_unlink = Path.unlink

    def _unlink_raises(self: Path, *args: object, **kwargs: object) -> None:
        if ".awf-symlink-cap-" in self.name:
            raise OSError("unlink busy")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _unlink_raises)
    with pytest.raises(OSError, match="unlink busy"):
        _worktree_filesystem_supports_symlinks(tmp_path)
    leftovers = list(tmp_path.glob(".awf-symlink-cap-*"))
    assert leftovers, "forced unlink failure must leave the probe for inspection"
    for leftover in leftovers:
        real_unlink(leftover, missing_ok=True)


@pytest.mark.unit
async def test_post_agent_symlink_read_hides_tamper_but_pre_agent_baseline_restores(
    tmp_path: Path,
) -> None:
    """Comment 3925551865: post-agent symlink reads must not define the baseline.

    An agent can flip ``core.symlinks=false`` and replace index symlinks with
    plain-file placeholders; a post-agent read then returns ``False`` and would
    skip ``-c core.symlinks=true``. Cleanup must honor the pre-agent baseline.
    """
    worktree = tmp_path / "worktree"
    restore_ref = _init_real_worktree(worktree, gitignore="")
    _run_real_git(worktree, "config", "core.symlinks", "true")
    link = worktree / "link"
    link.symlink_to("target")
    _run_real_git(worktree, "add", "link")
    _run_real_git(
        worktree,
        "-c",
        "user.email=awf@example.test",
        "-c",
        "user.name=AWF Test",
        "commit",
        "-m",
        "add link",
    )
    restore_ref = _run_real_git(worktree, "rev-parse", "HEAD").stdout.strip()
    pre_agent_baseline = await read_validation_worktree_symlink_form_baseline(
        _real_run_git(worktree),
        worktree,
    )
    assert pre_agent_baseline is True

    _run_real_git(worktree, "config", "core.symlinks", "false")
    link.unlink()
    link.write_bytes(b"target")

    post_agent_read = await read_validation_worktree_symlink_form_baseline(
        _real_run_git(worktree),
        worktree,
    )
    assert post_agent_read is False

    poisoned = _run_real_git(worktree, "status", "--porcelain", "--untracked-files=all")
    assert poisoned.stdout.strip() == ""

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=_real_run_git(worktree),
        worktree_path=worktree,
        restore_ref=restore_ref,
        trusted_index_symlinks_are_symlinks=pre_agent_baseline,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert link.is_symlink()
    assert link.readlink() == Path("target")


@pytest.mark.unit
@pytest.mark.parametrize("core_symlinks_value", ["no", "off", "0", ""])
async def test_cleanup_restores_symlink_for_git_false_aliases(
    tmp_path: Path,
    core_symlinks_value: str,
) -> None:
    """Bugbot 8734eacc: Git false aliases must un-hide symlink tampering."""
    worktree = tmp_path / "worktree"
    restore_ref = _init_real_worktree(worktree, gitignore="")
    _run_real_git(worktree, "config", "core.symlinks", "true")
    link = worktree / "link"
    link.symlink_to("target")
    _run_real_git(worktree, "add", "link")
    _run_real_git(
        worktree,
        "-c",
        "user.email=awf@example.test",
        "-c",
        "user.name=AWF Test",
        "commit",
        "-m",
        "add link",
    )
    restore_ref = _run_real_git(worktree, "rev-parse", "HEAD").stdout.strip()
    _run_real_git(worktree, "config", "core.symlinks", core_symlinks_value)
    link.unlink()
    link.write_bytes(b"target")

    poisoned = _run_real_git(worktree, "status", "--porcelain", "--untracked-files=all")
    assert poisoned.stdout.strip() == ""

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=_real_run_git(worktree),
        worktree_path=worktree,
        restore_ref=restore_ref,
        trusted_index_symlinks_are_symlinks=True,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert link.is_symlink()
    assert link.readlink() == Path("target")


@pytest.mark.unit
@pytest.mark.parametrize("core_symlinks_value", ["no", "off", "0", ""])
async def test_core_symlinks_enabled_treats_git_false_aliases_as_disabled(
    core_symlinks_value: str,
) -> None:
    async def run_git(args: list[str]) -> CommandResult:
        assert args == ["config", "--no-includes", "--bool", "--get", "core.symlinks"]
        return CommandResult(returncode=0, stdout="false\n", stderr="")

    assert await _core_symlinks_enabled(run_git) is False


@pytest.mark.unit
async def test_core_symlinks_enabled_treats_absent_key_as_enabled() -> None:
    """PRRT_kwDOSJAM6s6fIJuB: git ``--get`` exit 1 (absent) is the enabled default."""

    async def run_git(args: list[str]) -> CommandResult:
        assert args == ["config", "--no-includes", "--bool", "--get", "core.symlinks"]
        return CommandResult(returncode=1, stdout="", stderr="")

    assert await _core_symlinks_enabled(run_git) is True


@pytest.mark.unit
async def test_core_symlinks_enabled_raises_on_operational_failure() -> None:
    """PRRT_kwDOSJAM6s6fIJuB: timeout/non-absent failure fails closed, not enabled."""
    from awf.runtime.validation_worktree import _CoreSymlinksProbeError

    async def run_git(args: list[str]) -> CommandResult:
        assert args == ["config", "--no-includes", "--bool", "--get", "core.symlinks"]
        return CommandResult(
            returncode=124,
            stdout="",
            stderr="timed out",
            reason_code="COMMAND_TIMEOUT",
        )

    with pytest.raises(_CoreSymlinksProbeError, match="core.symlinks"):
        await _core_symlinks_enabled(run_git)


@pytest.mark.unit
async def test_check_clean_fails_closed_when_core_symlinks_probe_fails(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fIJuB: operational core.symlinks probe failure fails cleanliness.

    Treating probe failure as enabled would omit ``-c core.symlinks=true`` and let
    an agent hide a symlink→file typechange after restoring readable config.
    """
    worktree = tmp_path / "worktree"
    _init_real_worktree(worktree, gitignore="")
    real = _real_run_git(worktree)

    async def run_git(args: list[str]) -> CommandResult:
        if args == ["config", "--no-includes", "--bool", "--get", "core.symlinks"]:
            return CommandResult(
                returncode=124,
                stdout="",
                stderr="timed out",
                reason_code="COMMAND_TIMEOUT",
            )
        return await real(args)

    check = await check_validation_worktree_clean(
        run_git=run_git,
        worktree_path=worktree,
        ignore_all_ignored=True,
        trusted_index_symlinks_are_symlinks=True,
    )

    assert check.clean is False
    assert check.reason_code == VALIDATION_WORKTREE_STATUS_FAILED
    assert "core.symlinks" in (check.message or "")


@pytest.mark.unit
async def test_cleanup_fails_closed_when_core_symlinks_probe_fails(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fIJuB: cleanup must not proceed when core.symlinks is unreadable."""
    worktree = tmp_path / "worktree"
    restore_ref = _init_real_worktree(worktree, gitignore="")
    real = _real_run_git(worktree)

    async def run_git(args: list[str]) -> CommandResult:
        if args == ["config", "--no-includes", "--bool", "--get", "core.symlinks"]:
            return CommandResult(returncode=128, stdout="", stderr="fatal: bad config")
        return await real(args)

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git,
        worktree_path=worktree,
        restore_ref=restore_ref,
        trusted_index_symlinks_are_symlinks=True,
    )

    assert cleanup.cleaned is False
    assert cleanup.reason_code == VALIDATION_WORKTREE_STATUS_FAILED
    assert "core.symlinks" in (cleanup.message or "")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_core_symlinks_enabled_survives_include_path_fifo(tmp_path: Path) -> None:
    """PRRT_kwDOSJAM6s6e-r1k: live config lookup must not hang on include.path FIFO."""
    worktree = tmp_path / "worktree"
    _init_real_worktree(worktree, gitignore="")
    fifo = tmp_path / "poison.fifo"
    os.mkfifo(fifo, mode=0o644)
    subprocess.run(
        ["git", "config", "include.path", str(fifo)],
        cwd=worktree,
        check=True,
        capture_output=True,
    )

    runner = AsyncioSubprocessRunner()

    async def run_git(
        args: list[str],
        *,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        return await runner.run(
            ["git", "-C", str(worktree), *args],
            timeout_seconds=timeout_seconds,
        )

    assert await _core_symlinks_enabled(run_git) is True


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_check_validation_worktree_clean_times_out_on_include_path_fifo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6e-r1k: status probes must stay finite when includes poison config."""

    monkeypatch.setattr(validation_worktree_probes, "_VALIDATION_WORKTREE_GIT_TIMEOUT_SECONDS", 0.2)

    worktree = tmp_path / "worktree"
    _init_real_worktree(worktree, gitignore="")
    fifo = tmp_path / "status_poison.fifo"
    os.mkfifo(fifo, mode=0o644)
    subprocess.run(
        ["git", "config", "include.path", str(fifo)],
        cwd=worktree,
        check=True,
        capture_output=True,
    )

    runner = AsyncioSubprocessRunner()

    async def run_git(
        args: list[str],
        *,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        return await runner.run(
            ["git", "-C", str(worktree), *args],
            timeout_seconds=timeout_seconds,
        )

    check = await check_validation_worktree_clean(
        run_git=run_git,
        worktree_path=worktree,
        ignore_all_ignored=True,
    )

    assert check.clean is False
    assert check.reason_code == VALIDATION_WORKTREE_STATUS_FAILED


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_validation_git_forwards_timeout_to_executor_style_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bugbot 5104038224: executor git wrappers must accept timeout_seconds."""

    monkeypatch.setattr(
        validation_worktree_probes, "_VALIDATION_WORKTREE_GIT_TIMEOUT_SECONDS", 12.5
    )

    seen: list[float | None] = []

    async def locked_style_runner(
        args: list[str],
        *,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        seen.append(timeout_seconds)
        return CommandResult(returncode=0, stdout="", stderr="")

    await _run_validation_git(locked_style_runner, ["status", "--porcelain"])
    assert seen == [12.5]


@pytest.mark.unit
async def test_cleanup_restores_symlink_when_baseline_unset_and_core_symlinks_disabled(
    tmp_path: Path,
) -> None:
    """Bugbot 039bbf30: None baseline must fail-closed when symlinks are disabled."""
    worktree = tmp_path / "worktree"
    restore_ref = _init_real_worktree(worktree, gitignore="")
    _run_real_git(worktree, "config", "core.symlinks", "true")
    link = worktree / "link"
    link.symlink_to("target")
    _run_real_git(worktree, "add", "link")
    _run_real_git(
        worktree,
        "-c",
        "user.email=awf@example.test",
        "-c",
        "user.name=AWF Test",
        "commit",
        "-m",
        "add link",
    )
    restore_ref = _run_real_git(worktree, "rev-parse", "HEAD").stdout.strip()
    _run_real_git(worktree, "config", "core.symlinks", "false")
    link.unlink()
    link.write_bytes(b"target")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=_real_run_git(worktree),
        worktree_path=worktree,
        restore_ref=restore_ref,
        trusted_index_symlinks_are_symlinks=None,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert link.is_symlink()
    assert link.readlink() == Path("target")


@pytest.mark.unit
async def test_check_clean_honors_symlink_placeholder_checkout_when_core_symlinks_false(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6e8u_0: clean symlink placeholders must stay clean.

    When checkout legitimately uses ``core.symlinks=false``, index symlinks
    materialize as plain files. Forcing ``-c core.symlinks=true`` would report
    those placeholders as typechanges and cleanup would mutate the tree.
    """
    worktree = tmp_path / "worktree"
    _init_real_worktree(worktree, gitignore="")
    _run_real_git(worktree, "config", "core.symlinks", "true")
    link = worktree / "link"
    link.symlink_to("target")
    _run_real_git(worktree, "add", "link")
    _run_real_git(
        worktree,
        "-c",
        "user.email=awf@example.test",
        "-c",
        "user.name=AWF Test",
        "commit",
        "-m",
        "add link",
    )
    _run_real_git(worktree, "config", "core.symlinks", "false")
    link.unlink()
    link.write_bytes(b"target")

    forced = _run_real_git(
        worktree,
        "-c",
        "core.symlinks=true",
        "status",
        "--porcelain",
        "--untracked-files=all",
    )
    assert "link" in forced.stdout

    check = await check_validation_worktree_clean(
        run_git=_real_run_git(worktree),
        worktree_path=worktree,
        ignore_all_ignored=True,
        trusted_index_symlinks_are_symlinks=False,
    )

    assert check.reason_code is None
    assert check.clean is True


@pytest.mark.unit
async def test_check_clean_rejects_symlink_rematerialized_from_placeholder_baseline(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fMRYV: placeholder baseline must reject rematerialized symlinks.

    When the trusted checkout used ``core.symlinks=false`` placeholders, an agent
    can replace a placeholder with a real symlink of the same target text.
    Git then reports both forms clean under ``core.symlinks=false``, so
    validation must probe on-disk forms directly rather than relying on status.
    """
    worktree = tmp_path / "worktree"
    _init_real_worktree(worktree, gitignore="")
    _run_real_git(worktree, "config", "core.symlinks", "true")
    link = worktree / "link"
    link.symlink_to("target")
    _run_real_git(worktree, "add", "link")
    _run_real_git(
        worktree,
        "-c",
        "user.email=awf@example.test",
        "-c",
        "user.name=AWF Test",
        "commit",
        "-m",
        "add link",
    )
    _run_real_git(worktree, "config", "core.symlinks", "false")
    link.unlink()
    link.write_bytes(b"target")
    assert not link.is_symlink()

    # Equal-target rematerialization stays hidden from porcelain status.
    link.unlink()
    link.symlink_to("target")
    assert link.is_symlink()
    hidden = _run_real_git(worktree, "status", "--porcelain", "--untracked-files=all")
    assert hidden.stdout.strip() == ""

    check = await check_validation_worktree_clean(
        run_git=_real_run_git(worktree),
        worktree_path=worktree,
        ignore_all_ignored=True,
        trusted_index_symlinks_are_symlinks=False,
    )

    assert check.clean is False
    assert "link" in check.paths
    assert check.tracked_paths == ("link",)


@pytest.mark.unit
async def test_check_clean_fails_closed_when_placeholder_rematerialization_listing_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6fMRYV: listing failure must not skip rematerialization probe."""
    worktree = tmp_path / "worktree"
    _init_real_worktree(worktree, gitignore="")

    async def _listing_fails(_run_git: object) -> None:
        return None

    monkeypatch.setattr(
        "awf.runtime.validation_worktree_probes._index_symlink_paths",
        _listing_fails,
    )

    check = await check_validation_worktree_clean(
        run_git=_real_run_git(worktree),
        worktree_path=worktree,
        ignore_all_ignored=True,
        trusted_index_symlinks_are_symlinks=False,
    )

    assert check.clean is False
    assert check.reason_code == VALIDATION_WORKTREE_STATUS_FAILED
    assert "index symlinks" in check.message


@pytest.mark.unit
async def test_cleanup_fails_closed_when_rematerialized_symlink_unlink_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6fMRYV: unlink failure before restore must fail cleanup closed."""
    worktree = tmp_path / "worktree"
    restore_ref = _init_real_worktree(worktree, gitignore="")
    _run_real_git(worktree, "config", "core.symlinks", "true")
    link = worktree / "link"
    link.symlink_to("target")
    _run_real_git(worktree, "add", "link")
    _run_real_git(
        worktree,
        "-c",
        "user.email=awf@example.test",
        "-c",
        "user.name=AWF Test",
        "commit",
        "-m",
        "add link",
    )
    restore_ref = _run_real_git(worktree, "rev-parse", "HEAD").stdout.strip()
    _run_real_git(worktree, "config", "core.symlinks", "false")
    link.unlink()
    link.write_bytes(b"target")
    link.unlink()
    link.symlink_to("target")

    real_unlink = os.unlink

    def _unlink_fail(path: str | bytes | os.PathLike[str], *args: object, **kwargs: object) -> None:
        if os.fspath(path) == "link" or Path(os.fspath(path)).name == "link":
            raise OSError("permission denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", _unlink_fail)

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=_real_run_git(worktree),
        worktree_path=worktree,
        restore_ref=restore_ref,
        trusted_index_symlinks_are_symlinks=False,
    )

    assert cleanup.cleaned is False
    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert "rematerialized" in cleanup.message
    assert cleanup.cleanup_command == "unlink"


@pytest.mark.unit
async def test_cleanup_restores_symlink_rematerialized_from_placeholder_baseline(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fMRYV: cleanup must restore rematerialized placeholder symlinks."""
    worktree = tmp_path / "worktree"
    restore_ref = _init_real_worktree(worktree, gitignore="")
    _run_real_git(worktree, "config", "core.symlinks", "true")
    link = worktree / "link"
    link.symlink_to("target")
    _run_real_git(worktree, "add", "link")
    _run_real_git(
        worktree,
        "-c",
        "user.email=awf@example.test",
        "-c",
        "user.name=AWF Test",
        "commit",
        "-m",
        "add link",
    )
    restore_ref = _run_real_git(worktree, "rev-parse", "HEAD").stdout.strip()
    _run_real_git(worktree, "config", "core.symlinks", "false")
    link.unlink()
    link.write_bytes(b"target")
    assert not link.is_symlink()

    link.unlink()
    link.symlink_to("target")
    assert link.is_symlink()
    hidden = _run_real_git(worktree, "status", "--porcelain", "--untracked-files=all")
    assert hidden.stdout.strip() == ""

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=_real_run_git(worktree),
        worktree_path=worktree,
        restore_ref=restore_ref,
        trusted_index_symlinks_are_symlinks=False,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert "link" in cleanup.cleaned_paths
    assert link.exists()
    assert not link.is_symlink()
    assert link.read_bytes() == b"target"


@pytest.mark.unit
async def test_cleanup_restores_nested_rematerialized_symlink_nofollow(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fNhYT: nested rematerialized links still restore via nofollow unlink."""
    worktree = tmp_path / "worktree"
    restore_ref = _init_real_worktree(worktree, gitignore="")
    _run_real_git(worktree, "config", "core.symlinks", "true")
    nested = worktree / "nested"
    nested.mkdir()
    link = nested / "link"
    link.symlink_to("target")
    _run_real_git(worktree, "add", "nested/link")
    _run_real_git(
        worktree,
        "-c",
        "user.email=awf@example.test",
        "-c",
        "user.name=AWF Test",
        "commit",
        "-m",
        "add nested link",
    )
    restore_ref = _run_real_git(worktree, "rev-parse", "HEAD").stdout.strip()
    _run_real_git(worktree, "config", "core.symlinks", "false")
    link.unlink()
    link.write_bytes(b"target")
    assert not link.is_symlink()

    link.unlink()
    link.symlink_to("target")
    assert link.is_symlink()

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=_real_run_git(worktree),
        worktree_path=worktree,
        restore_ref=restore_ref,
        trusted_index_symlinks_are_symlinks=False,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert "nested/link" in cleanup.cleaned_paths
    assert link.exists()
    assert not link.is_symlink()
    assert link.read_bytes() == b"target"


@pytest.mark.unit
def test_worktree_symlink_probe_and_unlink_refuse_parent_symlink(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fNhYT: probe/unlink must not follow intermediate dir symlinks.

    ``Path.is_symlink`` / ``Path.unlink`` resolve parent components, so a swapped
    parent directory symlink can make cleanup delete a link outside the worktree.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "link"
    victim.symlink_to("keep-me")

    nested = worktree / "nested"
    nested.symlink_to(outside)

    # Pathname APIs follow the parent symlink and see the outside victim.
    assert (worktree / "nested" / "link").is_symlink()

    # Fail closed (None) or treat as absent — never True via the parent escape.
    assert _worktree_entry_is_symlink_nofollow(worktree, "nested/link") is not True
    with pytest.raises(OSError):
        _unlink_worktree_symlink_nofollow(worktree, "nested/link")
    assert victim.is_symlink()
    assert victim.readlink() == Path("keep-me")


@pytest.mark.unit
def test_worktree_symlink_probe_and_unlink_handle_in_tree_leaf(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fNhYT: nested in-tree rematerialized links still probe/unlink."""
    worktree = tmp_path / "worktree"
    nested = worktree / "nested"
    nested.mkdir(parents=True)
    link = nested / "link"
    link.symlink_to("target")

    assert _worktree_entry_is_symlink_nofollow(worktree, "nested/link") is True
    _unlink_worktree_symlink_nofollow(worktree, "nested/link")
    assert not link.exists()
    assert _worktree_entry_is_symlink_nofollow(worktree, "nested/link") is False


@pytest.mark.unit
async def test_cleanup_unlink_does_not_follow_parent_symlink_outside_worktree(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fNhYT: cleanup must not unlink via a parent directory symlink."""
    worktree = tmp_path / "worktree"
    restore_ref = _init_real_worktree(worktree, gitignore="")
    _run_real_git(worktree, "config", "core.symlinks", "true")
    nested = worktree / "nested"
    nested.mkdir()
    link = nested / "link"
    link.symlink_to("target")
    _run_real_git(worktree, "add", "nested/link")
    _run_real_git(
        worktree,
        "-c",
        "user.email=awf@example.test",
        "-c",
        "user.name=AWF Test",
        "commit",
        "-m",
        "add nested link",
    )
    restore_ref = _run_real_git(worktree, "rev-parse", "HEAD").stdout.strip()
    _run_real_git(worktree, "config", "core.symlinks", "false")
    link.unlink()
    link.write_bytes(b"target")
    assert not link.is_symlink()

    # Equal-target rematerialization (still under a real nested directory).
    link.unlink()
    link.symlink_to("target")
    assert link.is_symlink()

    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "link"
    victim.symlink_to("keep-me")

    # Swap the parent directory for a symlink out of the worktree after the
    # rematerialized leaf was observed. Pathname unlink would follow and delete
    # ``outside/link``.
    link.unlink()
    nested.rmdir()
    nested.symlink_to(outside)

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=_real_run_git(worktree),
        worktree_path=worktree,
        restore_ref=restore_ref,
        trusted_index_symlinks_are_symlinks=False,
    )

    assert victim.is_symlink()
    assert victim.readlink() == Path("keep-me")
    assert cleanup.cleaned is False
    assert cleanup.reason_code in {
        VALIDATION_WORKTREE_CLEANUP_FAILED,
        VALIDATION_WORKTREE_STATUS_FAILED,
    }


@pytest.mark.unit
async def test_placeholder_rematerialization_probe_ignores_parent_symlink(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fNhYT: rematerialization listing must not follow parents."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "link").symlink_to("target")
    nested = worktree / "nested"
    nested.symlink_to(outside)

    async def run_git(args: list[str]) -> CommandResult:
        assert args == ["ls-files", "-s", "-z"]
        return CommandResult(
            returncode=0,
            stdout="120000 cafebabe 0\tnested/link\0",
            stderr="",
        )

    # Pathname is_symlink would report True via the parent escape.
    assert (worktree / "nested" / "link").is_symlink()
    rematerialized = await _placeholder_baseline_rematerialized_symlink_paths(run_git, worktree)
    # Fail closed (None) or empty — never treat the escaped outside link as an
    # in-worktree rematerialization.
    assert rematerialized in (None, ())


@pytest.mark.unit
async def test_cleanup_restores_tracked_edit_when_core_fsmonitor_set(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6e0BJS: cleanup must restore edits when core.fsmonitor is set.

    Without ``-c core.fsmonitor=``, status omits the tracked edit after a
    primed fsmonitor hook, so rollback leaves mutated bytes behind.
    """
    worktree = tmp_path / "worktree"
    restore_ref = _init_real_worktree(worktree, gitignore="")
    target = worktree / "tracked.txt"
    target.write_text("original\n", encoding="utf-8")
    _run_real_git(worktree, "add", "tracked.txt")
    _run_real_git(
        worktree,
        "-c",
        "user.email=awf@example.test",
        "-c",
        "user.name=AWF Test",
        "commit",
        "-m",
        "add tracked",
    )
    restore_ref = _run_real_git(worktree, "rev-parse", "HEAD").stdout.strip()
    sentinel_script = tmp_path / "evil_fsmonitor.sh"
    sentinel_script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "version" ] || [ "$1" = "--query" ]; then\n'
        '  echo "1"\n'
        "  exit 0\n"
        "fi\n"
        'echo "last_update_token"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    sentinel_script.chmod(0o755)
    _run_real_git(worktree, "config", "core.fsmonitor", str(sentinel_script))
    _run_real_git(worktree, "status", "--porcelain")
    target.write_text("original\nmutated\n", encoding="utf-8")

    poisoned = _run_real_git(worktree, "status", "--porcelain", "--untracked-files=all")
    assert poisoned.stdout.strip() == ""

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=_real_run_git(worktree),
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert target.read_text(encoding="utf-8") == "original\n"


def _same_size_mtime_restored_edit(target: Path, *, worktree: Path) -> bytes:
    """Overwrite ``target`` with same-size bytes and restore its indexed mtime."""
    time.sleep(1.1)
    _run_real_git(worktree, "update-index", "--refresh")
    original = target.read_bytes()
    mtime_ns = target.stat().st_mtime_ns
    mutated = bytes((b ^ 0xFF) for b in original)
    assert len(mutated) == len(original)
    target.write_bytes(mutated)
    os.utime(target, ns=(mtime_ns, mtime_ns))
    return mutated


@pytest.mark.unit
async def test_cleanup_restores_tracked_edit_when_core_trustctime_false(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6e1yPZ: cleanup must restore same-size mtime-restored edits.

    Without ``-c core.trustctime=true``, status omits the tracked edit after a
    same-size overwrite that restores the indexed mtime, so rollback leaves
    mutated bytes behind.
    """
    worktree = tmp_path / "worktree"
    restore_ref = _init_real_worktree(worktree, gitignore="")
    target = worktree / "tracked.txt"
    target.write_text("original\n", encoding="utf-8")
    _run_real_git(worktree, "add", "tracked.txt")
    _run_real_git(
        worktree,
        "-c",
        "user.email=awf@example.test",
        "-c",
        "user.name=AWF Test",
        "commit",
        "-m",
        "add tracked",
    )
    restore_ref = _run_real_git(worktree, "rev-parse", "HEAD").stdout.strip()
    _run_real_git(worktree, "config", "core.trustctime", "false")
    mutated = _same_size_mtime_restored_edit(target, worktree=worktree)

    poisoned = _run_real_git(worktree, "status", "--porcelain", "--untracked-files=all")
    assert poisoned.stdout.strip() == ""
    assert target.read_bytes() == mutated

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=_real_run_git(worktree),
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert target.read_text(encoding="utf-8") == "original\n"


@pytest.mark.unit
async def test_cleanup_restores_tracked_edit_when_core_checkstat_minimal(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6e1yPZ: cleanup must restore edits under checkStat=minimal."""
    worktree = tmp_path / "worktree"
    restore_ref = _init_real_worktree(worktree, gitignore="")
    target = worktree / "tracked.txt"
    target.write_text("original\n", encoding="utf-8")
    _run_real_git(worktree, "add", "tracked.txt")
    _run_real_git(
        worktree,
        "-c",
        "user.email=awf@example.test",
        "-c",
        "user.name=AWF Test",
        "commit",
        "-m",
        "add tracked",
    )
    restore_ref = _run_real_git(worktree, "rev-parse", "HEAD").stdout.strip()
    _run_real_git(worktree, "config", "core.checkStat", "minimal")
    mutated = _same_size_mtime_restored_edit(target, worktree=worktree)

    poisoned = _run_real_git(worktree, "status", "--porcelain", "--untracked-files=all")
    assert poisoned.stdout.strip() == ""
    assert target.read_bytes() == mutated

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=_real_run_git(worktree),
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.reason_code is None
    assert cleanup.cleaned is True
    assert target.read_text(encoding="utf-8") == "original\n"
