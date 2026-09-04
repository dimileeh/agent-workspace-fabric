"""Sticky ``FakeCommandRunner`` responders for validation-worktree Git probes.

``check_validation_worktree_clean`` / ``cleanup_validation_worktree_side_effects``
run order-independent guard probes before every ``git status`` (``git config
--get core.symlinks`` and the index hide-flag listing ``git ls-files -v -z``),
and the executor captures a pre-agent symlink-form baseline with
``git ls-files -s -z``. Flow tests that script the commands they assert on with a
positional queue opt in here so those probes are answered without consuming a
queued result. Tests that exercise probe failures queue their own results
instead and must not call this helper.
"""

from __future__ import annotations

from awf.common.commands import FakeCommandRunner


def is_index_symlink_baseline_probe(args: list[str]) -> bool:
    """``git ls-files -s -z`` from the pre-agent symlink-form baseline capture."""
    return "ls-files" in args and "-s" in args and "-z" in args


def is_index_hide_flags_probe(args: list[str]) -> bool:
    """``git --literal-pathspecs ls-files -v -z`` from index hide-flag clearing."""
    return "ls-files" in args and "-v" in args and "-z" in args


def is_core_symlinks_probe(args: list[str]) -> bool:
    """``git config --no-includes --bool --get core.symlinks``."""
    return "config" in args and "--get" in args and "core.symlinks" in args


def answer_validation_git_probes(fake: FakeCommandRunner) -> None:
    """Answer the validation-worktree guard probes as a clean, default checkout.

    Empty ``ls-files`` output means no index symlinks (the baseline then uses the
    filesystem capability probe) and no hide flags (no ``update-index`` calls);
    ``core.symlinks`` unset (exit 1) is Git's enabled default.
    """
    fake.respond_when(is_index_symlink_baseline_probe, returncode=0, stdout="")
    fake.respond_when(is_index_hide_flags_probe, returncode=0, stdout="")
    fake.respond_when(is_core_symlinks_probe, returncode=1, stdout="")
