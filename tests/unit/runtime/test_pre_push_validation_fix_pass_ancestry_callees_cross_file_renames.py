"""Rename-aware cross-file callee evidence (issue #1019).

Unit tests for what a correction that *moves* the callee's file proves about
the reviewed call site: the range has to be diffed over both rename paths so a
pure move stays hunkless (PRRT_kwDOSJAM6s6q65JH), the callee has to survive at
the rename target (PRRT_kwDOSJAM6s6q8MWy), and the target has to stay reachable
through the unchanged caller's own import binding (PRRT_kwDOSJAM6s6q-L4B).
Split out of ``test_pre_push_validation_fix_pass_ancestry_callees_cross_file.py``
so both stay under the first-party file line limit.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.unit.runtime._pre_push_ancestry_cross_file_helpers import (
    _CALLEE_MODULE,
    _CALLEE_TEXT,
    _CALLER,
    _CALLER_TEXT,
    _LEFT,
    _RIGHT,
    _probe,
)

# The caller imports ``pkg_b.observability.execution_platform_metrics``, so a
# move *within* that module path — here the module becoming a package — leaves
# the unchanged import resolving to the moved definition.
_CALLEE_MODULE_RENAMED = "src/pkg_b/observability/execution_platform_metrics/__init__.py"

# A move *out* of the imported module path: the unchanged caller still imports
# ``pkg_b.observability.execution_platform_metrics``, which no longer exists.
_CALLEE_MODULE_MOVED_OUT = "src/pkg_b/metrics/execution_platform_metrics.py"

# What git prints for the rename's *old* path alone: the pathspec filters the new
# path out before rename detection, so the move reads as a whole-file deletion
# whose hunk overlaps every definition span in the file.
_RENAME_OLD_PATH_ONLY_DIFF = (
    f"diff --git a/{_CALLEE_MODULE} b/{_CALLEE_MODULE}\n"
    "deleted file mode 100644\n"
    f"--- a/{_CALLEE_MODULE}\n"
    "+++ /dev/null\n"
    f"@@ -1,{len(_CALLEE_TEXT.splitlines())} +0,0 @@\n"
    + "".join(f"-{line}\n" for line in _CALLEE_TEXT.splitlines())
)

# The same range diffed over *both* rename paths: a pure move has no hunks.
_PURE_RENAME_DIFF = (
    f"diff --git a/{_CALLEE_MODULE} b/{_CALLEE_MODULE_RENAMED}\n"
    "similarity index 100%\n"
    f"rename from {_CALLEE_MODULE}\n"
    f"rename to {_CALLEE_MODULE_RENAMED}\n"
)


def _rename_with_body_change_diff(target: str) -> str:
    """A move to ``target`` that also edits the callee's body (old line 6)."""
    return (
        f"diff --git a/{_CALLEE_MODULE} b/{target}\n"
        "similarity index 95%\n"
        f"rename from {_CALLEE_MODULE}\n"
        f"rename to {target}\n"
        f"--- a/{_CALLEE_MODULE}\n"
        f"+++ b/{target}\n"
        "@@ -6 +6 @@\n"
        "-    GAUGE.set(len(payload.entries))\n"
        "+    GAUGE.set(payload.ready_depth())\n"
    )


class _RenameProbe:
    """Runner stub that keys diffs by the whole pathspec, not just its last path.

    The rename-aware read passes ``-- <old> <new>``, so a probe keyed on
    ``cmd[-1]`` alone cannot tell it apart from a new-path-only read.
    """

    def __init__(
        self,
        *,
        name_status_z: str,
        diffs: dict[tuple[str, ...], str | None],
        rename_target: str = _CALLEE_MODULE_RENAMED,
        extra_texts: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self.name_status_z = name_status_z
        self.diffs = diffs
        self.diff_pathspecs: list[tuple[str, ...]] = []
        self.shows: list[tuple[str, str]] = []
        self.texts = {
            (_LEFT, _CALLER): _CALLER_TEXT,
            (_LEFT, _CALLEE_MODULE): _CALLEE_TEXT,
            # The move carries the definition to its rename target, which is
            # where the survival read looks for it (PRRT_kwDOSJAM6s6q8MWy).
            (_RIGHT, rename_target): _CALLEE_TEXT,
            **(extra_texts or {}),
        }
        self._deps = SimpleNamespace(runner=SimpleNamespace(run=self._run))

    async def _run(self, cmd: list[str], **kwargs: object):
        from awf.common.commands import CommandResult

        del kwargs
        if "show" in cmd:
            ref, _, path = cmd[-1].partition(":")
            self.shows.append((ref, path))
            text = self.texts.get((ref, path))
            if text is None:
                return CommandResult(returncode=128, stdout="", stderr="no such path")
            return CommandResult(returncode=0, stdout=text, stderr="")
        if "--name-status" in cmd:
            return CommandResult(returncode=0, stdout=self.name_status_z, stderr="")
        if "diff" in cmd:
            pathspec = tuple(cmd[cmd.index("--") + 1 :])
            self.diff_pathspecs.append(pathspec)
            diff = self.diffs.get(pathspec)
            if diff is None:
                return CommandResult(returncode=1, stdout="", stderr="diff failed")
            return CommandResult(returncode=0, stdout=diff, stderr="")
        # rev-list / rev-parse: no per-commit rename edges to add.
        return CommandResult(returncode=1, stdout="", stderr="unsupported")


def _name_status_z_rename(target: str) -> str:
    return f"R095\0{_CALLEE_MODULE}\0{target}\0"


_RENAME_NAME_STATUS_Z = _name_status_z_rename(_CALLEE_MODULE_RENAMED)


@pytest.mark.unit
async def test_pure_callee_file_rename_is_not_evidence() -> None:
    """A moved callee whose body is unchanged must not satisfy the gate.

    The changed-path set carries the rename's old path, and an old-path-only
    diff reads as a whole-file deletion overlapping the callee's span. Diffing
    both rename paths lets git pair the move, leaving no content hunk
    (PRRT_kwDOSJAM6s6q65JH).
    """
    probe = _RenameProbe(
        name_status_z=_RENAME_NAME_STATUS_Z,
        diffs={
            (_CALLEE_MODULE,): _RENAME_OLD_PATH_ONLY_DIFF,
            (_CALLEE_MODULE, _CALLEE_MODULE_RENAMED): _PURE_RENAME_DIFF,
        },
    )

    assert not await _probe(probe)
    assert (_CALLEE_MODULE, _CALLEE_MODULE_RENAMED) in probe.diff_pathspecs


@pytest.mark.unit
async def test_rename_that_also_changes_the_callee_body_is_evidence() -> None:
    """A move plus a real edit inside the definition still counts as FIXED evidence."""
    probe = _RenameProbe(
        name_status_z=_RENAME_NAME_STATUS_Z,
        diffs={
            (
                _CALLEE_MODULE,
                _CALLEE_MODULE_RENAMED,
            ): _rename_with_body_change_diff(_CALLEE_MODULE_RENAMED),
        },
    )

    assert await _probe(probe)
    # The callee's survival is read at the rename target, not at the old path.
    assert (_RIGHT, _CALLEE_MODULE_RENAMED) in probe.shows


@pytest.mark.unit
async def test_rename_aware_diff_failure_fails_closed() -> None:
    """An unreadable rename-aware diff is not evidence."""
    probe = _RenameProbe(name_status_z=_RENAME_NAME_STATUS_Z, diffs={})

    assert not await _probe(probe)


@pytest.mark.unit
async def test_rename_out_of_the_callers_import_module_is_not_evidence() -> None:
    """A callee moved out from under the caller's import binding is not a fix.

    The correction edits the callee's body *and* moves its file to a module the
    unchanged caller never imports, so the reviewed call site is now broken on
    its import rather than fixed. Following the rename target would resolve the
    actionable thread as ``fix_committed``, so the target has to stay reachable
    through the same binding that admitted the old path (PRRT_kwDOSJAM6s6q-L4B).
    """
    probe = _RenameProbe(
        name_status_z=_name_status_z_rename(_CALLEE_MODULE_MOVED_OUT),
        diffs={
            (
                _CALLEE_MODULE,
                _CALLEE_MODULE_MOVED_OUT,
            ): _rename_with_body_change_diff(_CALLEE_MODULE_MOVED_OUT),
        },
        rename_target=_CALLEE_MODULE_MOVED_OUT,
    )

    assert not await _probe(probe)
    # Rejected on the binding, before any overlap is read.
    assert not probe.diff_pathspecs


# The same correction that moves the callee also updates this caller's import.
_CALLER_TEXT_REIMPORTED = _CALLER_TEXT.replace(
    "pkg_b.observability.execution_platform_metrics",
    "pkg_b.metrics.execution_platform_metrics",
)


@pytest.mark.unit
async def test_rename_the_caller_reimports_in_the_same_range_is_evidence() -> None:
    """A move the corrected caller re-imports is still a fix of the call site.

    The left-side import no longer reaches the rename target, but this caller is
    not the unchanged one the fail-closed rule protects: the same range updates
    its import to the new module, so the callee stays reachable and the body
    edit inside its span is real evidence (PRRT_kwDOSJAM6s6q-L4B).
    """
    probe = _RenameProbe(
        name_status_z=_name_status_z_rename(_CALLEE_MODULE_MOVED_OUT) + f"M\0{_CALLER}\0",
        diffs={
            (
                _CALLEE_MODULE,
                _CALLEE_MODULE_MOVED_OUT,
            ): _rename_with_body_change_diff(_CALLEE_MODULE_MOVED_OUT),
        },
        rename_target=_CALLEE_MODULE_MOVED_OUT,
        extra_texts={(_RIGHT, _CALLER): _CALLER_TEXT_REIMPORTED},
    )

    assert await _probe(probe)


@pytest.mark.unit
async def test_rename_the_caller_drops_the_import_for_is_not_evidence() -> None:
    """A changed caller with no readable import for the callee fails closed.

    The right-side re-read exists for a caller whose import the correction
    *moved with* the callee; a caller that simply no longer imports the name
    must not fall back to the name-only rule and accept an arbitrary rename
    target (PRRT_kwDOSJAM6s6q-L4B).
    """
    probe = _RenameProbe(
        name_status_z=_name_status_z_rename(_CALLEE_MODULE_MOVED_OUT) + f"M\0{_CALLER}\0",
        diffs={
            (
                _CALLEE_MODULE,
                _CALLEE_MODULE_MOVED_OUT,
            ): _rename_with_body_change_diff(_CALLEE_MODULE_MOVED_OUT),
        },
        rename_target=_CALLEE_MODULE_MOVED_OUT,
        extra_texts={
            (_RIGHT, _CALLER): _CALLER_TEXT.split("\n", 1)[1],
        },
    )

    assert not await _probe(probe)


# A second file the same range renames, so the loop meets more than one move.
_SIBLING_MODULE = "src/pkg_b/observability/execution_platform_helpers.py"
_SIBLING_MOVED_OUT = "src/pkg_b/metrics/execution_platform_helpers.py"


@pytest.mark.unit
async def test_the_corrected_callers_binding_is_read_once_for_all_moves() -> None:
    """Several moves in one range share a single right-side read of the caller.

    The binding that can still admit a move is a property of the caller, not of
    the candidate, so re-reading it per renamed candidate would cost one ``git
    show`` each (PRRT_kwDOSJAM6s6q-L4B).
    """
    probe = _RenameProbe(
        name_status_z=(
            _name_status_z_rename(_CALLEE_MODULE_MOVED_OUT)
            + f"R095\0{_SIBLING_MODULE}\0{_SIBLING_MOVED_OUT}\0"
        ),
        diffs={
            (
                _CALLEE_MODULE,
                _CALLEE_MODULE_MOVED_OUT,
            ): _rename_with_body_change_diff(_CALLEE_MODULE_MOVED_OUT),
        },
        rename_target=_CALLEE_MODULE_MOVED_OUT,
    )

    assert not await _probe(probe)
    assert probe.shows.count((_RIGHT, _CALLER)) == 1
