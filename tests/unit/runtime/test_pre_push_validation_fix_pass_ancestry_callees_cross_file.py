"""Cross-file call-site→definition FIXED evidence helpers (issue #1019).

Unit tests for the two probes the correction attempt's fourth evidence gate is
built from: which definitions in *another* file are reachable from module scope,
and whether the item's own commit range changed the definition span of a callee
referenced at the reviewed line.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import FakeCommandRunner
from awf.runtime.pr_monitor_runner import (
    pre_push_validation_fix_pass_ancestry_cross_file as cross_file,
)

_LEFT = "a" * 40
_RIGHT = "b" * 40

_CALLER = "src/pkg_a/services/job_pools.py"
_CALLEE_MODULE = "src/pkg_b/observability/execution_platform_metrics.py"
_CALLEE_TEST = "tests/unit/observability/test_execution_platform_metrics.py"

# The reviewed file: the anchored line calls a helper defined in another package.
_CALLER_TEXT = (
    "from pkg_b.observability.execution_platform_metrics import record_ready_queue_depth\n"
    "\n"
    "\n"
    "def refresh_ready_queue_metrics(pool):\n"
    "    payload = pool.snapshot()\n"
    "    record_ready_queue_depth(payload)\n"
    "    return payload\n"
)
_ANCHOR_LINE = 6

# The callee's module: ``record_ready_queue_depth`` spans lines 4-7.
_CALLEE_TEXT = (
    "GAUGE = None\n"
    "\n"
    "\n"
    "def record_ready_queue_depth(payload):\n"
    '    """Record the ready-queue depth."""\n'
    "    GAUGE.set(len(payload.entries))\n"
    "    return None\n"
    "\n"
    "\n"
    "def unrelated_helper():\n"
    "    return 0\n"
)

# A body-only change inside ``record_ready_queue_depth``'s span (old line 6).
_IN_SPAN_DIFF = (
    f"--- a/{_CALLEE_MODULE}\n"
    f"+++ b/{_CALLEE_MODULE}\n"
    "@@ -6 +6 @@\n"
    "-    GAUGE.set(len(payload.entries))\n"
    "+    GAUGE.set(payload.ready_depth())\n"
)

# A change in the same file, far from the callee's span (old line 11).
_OUT_OF_SPAN_DIFF = (
    f"--- a/{_CALLEE_MODULE}\n+++ b/{_CALLEE_MODULE}\n@@ -11 +11 @@\n-    return 0\n+    return 1\n"
)


def _name_status_z(*paths: str) -> str:
    return "".join(f"M\0{path}\0" for path in paths)


class _Probe:
    """A runner-shaped stub answering the four git reads the probe issues."""

    def __init__(
        self,
        *,
        texts: dict[tuple[str, str], str | None],
        changed_paths: tuple[str, ...] | None,
        diffs: dict[str, str | None],
    ) -> None:
        self.texts = texts
        self.changed_paths = changed_paths
        self.diffs = diffs
        self.runner = FakeCommandRunner()
        self.shows: list[str] = []
        self.diff_paths: list[str] = []
        self._deps = SimpleNamespace(runner=SimpleNamespace(run=self._run))

    async def _run(self, cmd: list[str], **kwargs: object):
        from awf.common.commands import CommandResult

        del kwargs
        if "show" in cmd:
            ref, _, path = cmd[-1].partition(":")
            self.shows.append(path)
            text = self.texts.get((ref, path))
            if text is None:
                return CommandResult(returncode=128, stdout="", stderr="no such path")
            return CommandResult(returncode=0, stdout=text, stderr="")
        if "--name-status" in cmd:
            if self.changed_paths is None:
                return CommandResult(returncode=1, stdout="", stderr="diff failed")
            return CommandResult(
                returncode=0, stdout=_name_status_z(*self.changed_paths), stderr=""
            )
        path = cmd[-1]
        self.diff_paths.append(path)
        diff = self.diffs.get(path)
        if diff is None:
            return CommandResult(returncode=1, stdout="", stderr="diff failed")
        return CommandResult(returncode=0, stdout=diff, stderr="")


def _cross_package_probe(
    *,
    caller_text: str | None = _CALLER_TEXT,
    callee_text: str | None = _CALLEE_TEXT,
    changed_paths: tuple[str, ...] | None = (_CALLEE_MODULE, _CALLEE_TEST),
    diff: str | None = _IN_SPAN_DIFF,
) -> _Probe:
    return _Probe(
        texts={
            (_LEFT, _CALLER): caller_text,
            (_LEFT, _CALLEE_MODULE): callee_text,
            (_LEFT, _CALLEE_TEST): "def test_record():\n    assert True\n",
        },
        changed_paths=changed_paths,
        diffs={_CALLEE_MODULE: diff, _CALLEE_TEST: ""},
    )


async def _probe(
    runner: object,
    *,
    item_path: str = _CALLER,
    item_line: int = _ANCHOR_LINE,
    worktree_path: Path | None = None,
) -> bool:
    return await cross_file._commit_range_changes_callee_definition(
        runner,
        worktree_path=worktree_path or Path("/tmp/ws"),
        left=_LEFT,
        right=_RIGHT,
        item_path=item_path,
        item_line=item_line,
    )


# --- _importable_definition_spans_for_names ---------------------------------


@pytest.mark.unit
def test_module_level_definitions_are_importable() -> None:
    """Module-level ``def`` / ``class`` / arrow heads are reachable by name."""
    spans = cross_file._importable_definition_spans_for_names(
        _CALLEE_TEXT,
        frozenset({"record_ready_queue_depth"}),
        path=_CALLEE_MODULE,
    )
    # Spans run through trailing blank lines, exactly as on attempt 0.
    assert spans == [(4, 9)]

    text = "class Gauge:\n    pass\n"
    assert cross_file._importable_definition_spans_for_names(
        text, frozenset({"Gauge"}), path="src/pkg/g.py"
    ) == [(1, 2)]

    arrow = "const helper = (value) => {\n  return value;\n};\n"
    assert cross_file._importable_definition_spans_for_names(
        arrow, frozenset({"helper"}), path="src/pkg/g.ts"
    ) == [(1, 3)]


@pytest.mark.unit
def test_class_members_are_importable_but_function_locals_are_not() -> None:
    """``Class.method`` is reachable from module scope; a closure is not."""
    text = (
        "class Collector:\n"
        "    def record(self, payload):\n"
        "        return payload\n"
        "\n"
        "    class Inner:\n"
        "        def record_inner(self):\n"
        "            return 1\n"
        "\n"
        "\n"
        "def outer():\n"
        "    def record_local():\n"
        "        return 2\n"
        "\n"
        "    return record_local()\n"
    )
    assert cross_file._importable_definition_spans_for_names(
        text, frozenset({"record"}), path="src/pkg/c.py"
    ) == [(2, 4)]
    assert cross_file._importable_definition_spans_for_names(
        text, frozenset({"record_inner"}), path="src/pkg/c.py"
    ) == [(6, 9)]
    assert (
        cross_file._importable_definition_spans_for_names(
            text, frozenset({"record_local"}), path="src/pkg/c.py"
        )
        == []
    )


@pytest.mark.unit
def test_block_scoped_and_unmatched_names_are_rejected() -> None:
    """Indented JS/TS or assignment heads are block-scoped; misses return ``[]``."""
    js = "if (flag) {\n  function helper() {\n    return 1;\n  }\n}\n"
    assert (
        cross_file._importable_definition_spans_for_names(
            js, frozenset({"helper"}), path="src/pkg/g.ts"
        )
        == []
    )
    py_assignment = "if flag:\n    helper = lambda value: value\n"
    assert (
        cross_file._importable_definition_spans_for_names(
            py_assignment, frozenset({"helper"}), path="src/pkg/g.py"
        )
        == []
    )
    assert (
        cross_file._importable_definition_spans_for_names(
            _CALLEE_TEXT, frozenset({"absent_name"}), path=_CALLEE_MODULE
        )
        == []
    )


@pytest.mark.unit
def test_empty_names_or_text_short_circuit() -> None:
    """No callee names and no file text both mean "nothing to resolve"."""
    assert (
        cross_file._importable_definition_spans_for_names(
            _CALLEE_TEXT, frozenset(), path=_CALLEE_MODULE
        )
        == []
    )
    assert (
        cross_file._importable_definition_spans_for_names(
            "", frozenset({"record_ready_queue_depth"}), path=_CALLEE_MODULE
        )
        == []
    )


@pytest.mark.unit
def test_docstring_decoys_stay_masked() -> None:
    """A definition head inside a docstring is not an importable definition."""
    text = (
        '"""Module docs.\n'
        "\n"
        "def record_ready_queue_depth(payload):\n"
        "    return payload\n"
        '"""\n'
        "\n"
        "VALUE = 1\n"
    )
    assert (
        cross_file._importable_definition_spans_for_names(
            text, frozenset({"record_ready_queue_depth"}), path=_CALLEE_MODULE
        )
        == []
    )


# --- _commit_range_changes_callee_definition ---------------------


@pytest.mark.unit
async def test_cross_file_callee_definition_change_is_evidence() -> None:
    """The #1019 shape: the range changes the callee's body in another package."""
    probe = _cross_package_probe()

    assert await _probe(probe)
    # The reviewed path is never re-diffed: same-path is the earlier gates' job.
    assert _CALLER not in probe.diff_paths


@pytest.mark.unit
async def test_a_change_outside_the_definition_span_is_not_evidence() -> None:
    """Touching the callee's *file* is not enough; the diff must hit the span."""
    probe = _cross_package_probe(diff=_OUT_OF_SPAN_DIFF)

    assert not await _probe(probe)


@pytest.mark.unit
async def test_no_callee_relationship_is_not_evidence() -> None:
    """A changed file that defines nothing the anchored line calls fails closed."""
    probe = _cross_package_probe(
        callee_text="def unrelated_helper():\n    return 0\n",
        diff=(
            f"--- a/{_CALLEE_MODULE}\n"
            f"+++ b/{_CALLEE_MODULE}\n"
            "@@ -2 +2 @@\n"
            "-    return 0\n"
            "+    return 1\n"
        ),
    )

    assert not await _probe(probe)


@pytest.mark.unit
async def test_self_cls_and_this_qualifiers_fail_closed() -> None:
    """Receivers attempt 0 resolves in-file must not resolve across files."""
    for qualifier, path, callee_path in (
        ("self", "src/pkg_a/services/pools.py", _CALLEE_MODULE),
        ("cls", "src/pkg_a/services/pools.py", _CALLEE_MODULE),
        ("this", "src/pkg_a/services/pools.ts", "src/pkg_b/metrics.ts"),
    ):
        caller = (
            "class Pools:\n"
            "    def refresh(self):\n"
            f"        return {qualifier}.record_ready_queue_depth(1)\n"
        )
        probe = _Probe(
            texts={(_LEFT, path): caller, (_LEFT, callee_path): _CALLEE_TEXT},
            changed_paths=(callee_path,),
            diffs={callee_path: _IN_SPAN_DIFF},
        )
        assert not await _probe(probe, item_path=path, item_line=3)


@pytest.mark.unit
async def test_non_self_attribute_qualifiers_still_resolve_by_name() -> None:
    """``metrics.record_ready_queue_depth()`` is exactly the cross-file shape."""
    caller = (
        "from pkg_b.observability import execution_platform_metrics as metrics\n"
        "\n"
        "\n"
        "def refresh(payload):\n"
        "    metrics.record_ready_queue_depth(payload)\n"
    )
    probe = _cross_package_probe(caller_text=caller)

    assert await _probe(probe, item_line=5)


@pytest.mark.unit
async def test_unreadable_inputs_fail_closed() -> None:
    """An unreadable reviewed file, an empty path/line, or a failed diff → False."""
    assert not await _probe(_cross_package_probe(caller_text=None))
    assert not await _probe(_cross_package_probe(), item_path="   ")
    assert not await _probe(_cross_package_probe(), item_line=0)
    assert not await _probe(_cross_package_probe(changed_paths=None))
    assert not await _probe(_cross_package_probe(diff=None))
    # A changed path whose text cannot be read at ``left`` (newly added file).
    assert not await _probe(_cross_package_probe(callee_text=None))


@pytest.mark.unit
async def test_only_the_reviewed_path_changed_is_not_evidence() -> None:
    """With nothing but the reviewed path in the range there is no candidate."""
    probe = _Probe(
        texts={(_LEFT, _CALLER): _CALLER_TEXT},
        changed_paths=(_CALLER,),
        diffs={},
    )

    assert not await _probe(probe)
    assert probe.diff_paths == []


@pytest.mark.unit
async def test_candidate_path_count_is_capped() -> None:
    """A pathological range is bounded; the callee past the cap is not probed."""
    bulk = tuple(f"src/pkg_c/mod_{index:03d}.py" for index in range(40))
    probe = _Probe(
        texts={
            (_LEFT, _CALLER): _CALLER_TEXT,
            **{(_LEFT, path): "VALUE = 1\n" for path in bulk},
            (_LEFT, _CALLEE_MODULE): _CALLEE_TEXT,
        },
        changed_paths=(*bulk, _CALLEE_MODULE),
        diffs={_CALLEE_MODULE: _IN_SPAN_DIFF},
    )

    assert not await _probe(probe)
    assert len(probe.shows) == 1 + cross_file._MAX_CALLEE_EVIDENCE_CANDIDATE_PATHS
    assert _CALLEE_MODULE not in probe.shows


@pytest.mark.unit
async def test_raw_diff_bytes_are_preferred_over_replacement_decoded_stdout() -> None:
    """Exact diff bytes win, as in the attempt-0 probe, so decode damage cannot hide a hunk."""
    from awf.common.commands import CommandResult

    class _BytesProbe(_Probe):
        async def _run(self, cmd: list[str], **kwargs: object):
            if "diff" in cmd and "-U0" in cmd:
                self.diff_paths.append(cmd[-1])
                return CommandResult(
                    returncode=0,
                    stdout="\ufffd lossy",
                    stderr="",
                    stdout_bytes=_IN_SPAN_DIFF.encode("utf-8"),
                )
            return await super()._run(cmd, **kwargs)

    probe = _BytesProbe(
        texts={
            (_LEFT, _CALLER): _CALLER_TEXT,
            (_LEFT, _CALLEE_MODULE): _CALLEE_TEXT,
        },
        changed_paths=(_CALLEE_MODULE,),
        diffs={},
    )

    assert await _probe(probe)


# --- rename-aware candidate diffs (PRRT_kwDOSJAM6s6q65JH) --------------------

_CALLEE_MODULE_RENAMED = "src/pkg_b/metrics/execution_platform_metrics.py"

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

# A move that also edits the callee's body (old line 6) is still real evidence.
_RENAME_WITH_BODY_CHANGE_DIFF = (
    f"diff --git a/{_CALLEE_MODULE} b/{_CALLEE_MODULE_RENAMED}\n"
    "similarity index 95%\n"
    f"rename from {_CALLEE_MODULE}\n"
    f"rename to {_CALLEE_MODULE_RENAMED}\n"
    f"--- a/{_CALLEE_MODULE}\n"
    f"+++ b/{_CALLEE_MODULE_RENAMED}\n"
    "@@ -6 +6 @@\n"
    "-    GAUGE.set(len(payload.entries))\n"
    "+    GAUGE.set(payload.ready_depth())\n"
)


class _RenameProbe:
    """Runner stub that keys diffs by the whole pathspec, not just its last path.

    The rename-aware read passes ``-- <old> <new>``, so a probe keyed on
    ``cmd[-1]`` alone cannot tell it apart from a new-path-only read.
    """

    def __init__(self, *, name_status_z: str, diffs: dict[tuple[str, ...], str | None]) -> None:
        self.name_status_z = name_status_z
        self.diffs = diffs
        self.diff_pathspecs: list[tuple[str, ...]] = []
        self.texts = {
            (_LEFT, _CALLER): _CALLER_TEXT,
            (_LEFT, _CALLEE_MODULE): _CALLEE_TEXT,
        }
        self._deps = SimpleNamespace(runner=SimpleNamespace(run=self._run))

    async def _run(self, cmd: list[str], **kwargs: object):
        from awf.common.commands import CommandResult

        del kwargs
        if "show" in cmd:
            ref, _, path = cmd[-1].partition(":")
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


_RENAME_NAME_STATUS_Z = f"R095\0{_CALLEE_MODULE}\0{_CALLEE_MODULE_RENAMED}\0"


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
            (_CALLEE_MODULE, _CALLEE_MODULE_RENAMED): _RENAME_WITH_BODY_CHANGE_DIFF,
        },
    )

    assert await _probe(probe)


@pytest.mark.unit
async def test_rename_aware_diff_failure_fails_closed() -> None:
    """An unreadable rename-aware diff is not evidence."""
    probe = _RenameProbe(name_status_z=_RENAME_NAME_STATUS_Z, diffs={})

    assert not await _probe(probe)
