"""Shared probe builders for the cross-file callee-evidence tests (issue #1019).

Extracted from ``test_pre_push_validation_fix_pass_ancestry_callees_cross_file.py``
so that module and its receiver-binding sibling stay under the first-party file
line limit enforced by ``tests/unit/test_core_decomposition_maintainability.py``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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
            if (ref, path) in self.texts:
                text = self.texts[(ref, path)]
            elif ref == _RIGHT:
                # The accepted candidate is read back on the right to check the
                # callee survived the correction; unless a case says the
                # definition went away, the right side still holds it.
                text = self.texts.get((_LEFT, path))
            else:
                text = None
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
