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
def test_bare_callee_names_do_not_link_class_methods() -> None:
    """A bare cross-module call binds a module-scope name, never ``Class.method``.

    ``from mod import record`` cannot reach a method of ``mod.Collector``, so an
    unrelated same-named method must not satisfy the gate for a bare ``record()``
    call site (PRRT_kwDOSJAM6s6q699Q). Attribute receivers keep resolving it:
    ``collector.record()`` is exactly that shape.
    """
    text = (
        "class Collector:\n"
        "    def record(self, payload):\n"
        "        return payload\n"
        "\n"
        "\n"
        "def record_module_level(payload):\n"
        "    return payload\n"
    )
    assert (
        cross_file._importable_definition_spans_for_names(
            text, frozenset(), path="src/pkg/c.py", bare_names=frozenset({"record"})
        )
        == []
    )
    assert cross_file._importable_definition_spans_for_names(
        text, frozenset({"record"}), path="src/pkg/c.py"
    ) == [(2, 5)]
    # A module-level def is reachable for both call shapes.
    assert cross_file._importable_definition_spans_for_names(
        text, frozenset(), path="src/pkg/c.py", bare_names=frozenset({"record_module_level"})
    ) == [(6, 7)]


@pytest.mark.unit
def test_a_name_called_both_bare_and_qualified_keeps_the_attribute_rule() -> None:
    """One anchored line may hold both shapes; the attribute shape still resolves."""
    text = "class Collector:\n    def record(self, payload):\n        return payload\n"
    assert cross_file._importable_definition_spans_for_names(
        text,
        frozenset({"record"}),
        path="src/pkg/c.py",
        bare_names=frozenset({"record"}),
    ) == [(2, 3)]


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


@pytest.mark.unit
def test_contiguous_decorators_belong_to_the_definition_span() -> None:
    """A decorator-only correction must land inside the callee's span.

    ``@staticmethod`` → ``@classmethod`` and retry/auth decorator edits change
    the callee without touching its ``def`` line or body, so the span starts at
    the topmost contiguous decorator (PRRT_kwDOSJAM6s6q791u). Multiline
    decorator call tails and blank/comment gaps stay inside the stack.
    """
    text = (
        "GAUGE = None\n"
        "\n"
        "@retry(\n"
        "    times=3,\n"
        ")\n"
        "# keep the gauge hot\n"
        "@audit\n"
        "def record_ready_queue_depth(payload):\n"
        "    return GAUGE\n"
    )
    assert cross_file._importable_definition_spans_for_names(
        text, frozenset({"record_ready_queue_depth"}), path=_CALLEE_MODULE
    ) == [(3, 9)]


@pytest.mark.unit
def test_statements_above_an_undecorated_definition_stay_outside_the_span() -> None:
    """Only decorators extend the head: ordinary code above it is not the callee."""
    text = "VALUE = compute(\n    1,\n)\ndef record_ready_queue_depth(payload):\n    return VALUE\n"
    assert cross_file._importable_definition_spans_for_names(
        text, frozenset({"record_ready_queue_depth"}), path=_CALLEE_MODULE
    ) == [(4, 5)]


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
async def test_a_decorator_only_change_to_the_callee_is_evidence() -> None:
    """Switching the callee's decorator is a change to the callee's definition."""
    callee = (
        "GAUGE = None\n"
        "\n"
        "\n"
        "@staticmethod\n"
        "def record_ready_queue_depth(payload):\n"
        "    return GAUGE\n"
    )
    probe = _cross_package_probe(
        callee_text=callee,
        diff=(
            f"--- a/{_CALLEE_MODULE}\n"
            f"+++ b/{_CALLEE_MODULE}\n"
            "@@ -4 +4 @@\n"
            "-@staticmethod\n"
            "+@classmethod\n"
        ),
    )

    assert await _probe(probe)


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
async def test_bare_call_site_is_not_linked_to_an_unrelated_class_method() -> None:
    """A bare ``record_ready_queue_depth()`` must not link a same-named method."""
    callee = (
        "class Collector:\n"
        "    def record_ready_queue_depth(self, payload):\n"
        "        return payload\n"
    )
    probe = _cross_package_probe(
        callee_text=callee,
        diff=(
            f"--- a/{_CALLEE_MODULE}\n"
            f"+++ b/{_CALLEE_MODULE}\n"
            "@@ -3 +3 @@\n"
            "-        return payload\n"
            "+        return payload.entries\n"
        ),
    )

    assert not await _probe(probe)


_UNRELATED_SAME_NAME = "src/pkg_c/unrelated.py"


def _unrelated_same_name_probe(*, caller_text: str = _CALLER_TEXT) -> _Probe:
    """A commit range that changed a same-named def in a module nobody imported."""
    return _Probe(
        texts={
            (_LEFT, _CALLER): caller_text,
            (_LEFT, _UNRELATED_SAME_NAME): _CALLEE_TEXT,
        },
        changed_paths=(_UNRELATED_SAME_NAME,),
        diffs={_UNRELATED_SAME_NAME: _IN_SPAN_DIFF.replace(_CALLEE_MODULE, _UNRELATED_SAME_NAME)},
    )


@pytest.mark.unit
async def test_bare_callee_import_binding_rejects_an_unrelated_same_named_def() -> None:
    """A bare callee resolves only under the module its import names.

    ``from pkg_b.observability.execution_platform_metrics import
    record_ready_queue_depth`` cannot reach another module-level
    ``record_ready_queue_depth``, so editing one in ``pkg_c`` is not evidence
    that the reviewed call site was fixed (PRRT_kwDOSJAM6s6q7bSI).
    """
    assert not await _probe(_unrelated_same_name_probe())


@pytest.mark.unit
async def test_bare_callee_accepts_a_definition_under_the_imported_package() -> None:
    """A package import re-exporting the callee still resolves to its submodule."""
    caller = (
        "from pkg_b.observability import record_ready_queue_depth\n"
        "\n"
        "\n"
        "def refresh(payload):\n"
        "    record_ready_queue_depth(payload)\n"
    )

    assert await _probe(_cross_package_probe(caller_text=caller), item_line=5)


@pytest.mark.unit
async def test_bare_callee_alias_binding_follows_the_aliased_module() -> None:
    """``import helper as record`` binds the *alias*, so the alias' module wins."""
    caller = (
        "from pkg_c.unrelated import helper as record_ready_queue_depth\n"
        "\n"
        "\n"
        "def refresh(payload):\n"
        "    record_ready_queue_depth(payload)\n"
    )

    assert not await _probe(_cross_package_probe(caller_text=caller), item_line=5)


@pytest.mark.unit
async def test_qualified_receiver_binding_rejects_an_unrelated_same_named_def() -> None:
    """A receiver resolves only under the module its own import names.

    ``from pkg_b.observability import ... as metrics`` cannot reach a
    ``record_ready_queue_depth`` defined in ``pkg_c``, so editing that one is no
    more evidence for ``metrics.record_ready_queue_depth()`` than for the bare
    call shape (PRRT_kwDOSJAM6s6q7bSI).
    """
    caller = (
        "from pkg_b.observability import execution_platform_metrics as metrics\n"
        "\n"
        "\n"
        "def refresh(payload):\n"
        "    metrics.record_ready_queue_depth(payload)\n"
    )

    assert not await _probe(_unrelated_same_name_probe(caller_text=caller), item_line=5)


@pytest.mark.unit
async def test_qualified_receiver_bound_by_a_plain_import_resolves_to_that_module() -> None:
    """``import pkg.mod`` binds the receiver ``pkg.mod.record()`` calls through."""
    caller = (
        "import pkg_b.observability.execution_platform_metrics\n"
        "\n"
        "\n"
        "def refresh(payload):\n"
        "    pkg_b.observability.execution_platform_metrics"
        ".record_ready_queue_depth(payload)\n"
    )

    assert await _probe(_cross_package_probe(caller_text=caller), item_line=5)
    assert not await _probe(_unrelated_same_name_probe(caller_text=caller), item_line=5)


@pytest.mark.unit
async def test_qualified_receiver_alias_follows_the_aliased_module() -> None:
    """``import pkg_c.unrelated as metrics`` binds the *alias* to ``pkg_c``."""
    caller = (
        "import pkg_c.unrelated as metrics\n"
        "\n"
        "\n"
        "def refresh(payload):\n"
        "    metrics.record_ready_queue_depth(payload)\n"
    )

    assert not await _probe(_cross_package_probe(caller_text=caller), item_line=5)


@pytest.mark.unit
async def test_qualified_receiver_without_a_binding_keeps_the_name_only_rule() -> None:
    """A receiver that is a parameter rather than an import narrows nothing.

    Its definition's module is unknowable without type resolution, so the #1019
    shape keeps resolving by name instead of being re-parked as needs_human.
    """
    caller = "def refresh(payload, metrics):\n    metrics.record_ready_queue_depth(payload)\n"

    assert await _probe(_cross_package_probe(caller_text=caller), item_line=2)


@pytest.mark.unit
def test_plain_import_bindings_skip_pieces_that_are_not_module_names() -> None:
    """Only dotted module names bind; the trailing-comma/garbage pieces do not."""
    assert cross_file._plain_import_module_paths(
        "import a.b  # note\nimport a.b as c\nimport (\n", path="m.py"
    ) == {"b": frozenset({"a/b"}), "c": frozenset({"a/b"})}
    # Non-Python call sites keep the name-only rule for receivers too.
    assert cross_file._plain_import_module_paths("import a.b\n", path="m.ts") == {}


@pytest.mark.unit
async def test_bare_callee_without_a_resolvable_import_keeps_the_name_only_rule() -> None:
    """Star, over-deep and plain-``import`` bindings carry no path to match.

    A star import binds no name AWF can read, a relative import that climbs past
    the repo root resolves to nothing, and ``import M`` binds ``M`` rather than
    the callee. None narrows the candidate, so all keep the name-only rule rather
    than failing closed and re-parking the #1019 fixes.
    """
    for header in (
        "from pkg_b.observability.execution_platform_metrics import *",
        "from ......execution_platform_metrics import record_ready_queue_depth",
        "import pkg_b.observability.execution_platform_metrics",
    ):
        caller = f"{header}\n\n\ndef refresh(payload):\n    record_ready_queue_depth(payload)\n"

        assert await _probe(_cross_package_probe(caller_text=caller), item_line=5)


@pytest.mark.unit
async def test_relative_import_binding_rejects_a_module_it_cannot_reach() -> None:
    """``from .sibling import x`` resolves against the call site's own directory.

    The target is the caller's package, so a same-named definition in another
    package is no more reachable than through a mismatched absolute import
    (PRRT_kwDOSJAM6s6q7bSI).
    """
    caller = (
        "from .execution_platform_metrics import record_ready_queue_depth\n"
        "\n"
        "\n"
        "def refresh(payload):\n"
        "    record_ready_queue_depth(payload)\n"
    )

    assert not await _probe(_cross_package_probe(caller_text=caller), item_line=5)


@pytest.mark.unit
async def test_relative_import_accepts_the_module_its_dots_climb_to() -> None:
    """A relative import that does reach the callee's module still resolves.

    ``src/pkg_a/services/job_pools.py`` + ``from ...pkg_b...`` climbs to ``src/``,
    which is exactly where the changed callee module lives.
    """
    caller = (
        "from ...pkg_b.observability.execution_platform_metrics import (\n"
        "    record_ready_queue_depth,\n"
        ")\n"
        "\n"
        "\n"
        "def refresh(payload):\n"
        "    record_ready_queue_depth(payload)\n"
    )

    assert await _probe(_cross_package_probe(caller_text=caller), item_line=7)


@pytest.mark.unit
def test_relative_import_targets_resolve_against_the_call_site_directory() -> None:
    """``from . import x`` binds the package itself; a too-deep climb binds nothing."""
    assert cross_file._relative_import_module_path(_CALLER, ".", None) == "src/pkg_a/services"
    assert (
        cross_file._relative_import_module_path(_CALLER, "..", "obs.metrics")
        == "src/pkg_a/obs/metrics"
    )
    assert cross_file._relative_import_module_path(_CALLER, "." * 5, "metrics") is None
    # A module at the repo root has no package segments for ``from . import`` to name.
    assert cross_file._relative_import_module_path("job_pools.py", ".", None) is None


@pytest.mark.unit
async def test_parenthesized_import_blocks_bind_every_name_they_list() -> None:
    """The wrapped ``from M import (a, b)`` form is the one ruff emits."""
    caller = (
        "from pkg_c.unrelated import (\n"
        "    helper,\n"
        "    record_ready_queue_depth,\n"
        ")\n"
        "\n"
        "\n"
        "def refresh(payload):\n"
        "    record_ready_queue_depth(payload)\n"
    )

    assert not await _probe(_cross_package_probe(caller_text=caller), item_line=8)


@pytest.mark.unit
def test_a_module_path_deeper_than_the_candidate_cannot_match() -> None:
    """An import of a deep submodule is not satisfied by a shallower file."""
    assert not cross_file._candidate_is_under_module_path(
        "src/pkg_b/metrics.py", "pkg_b/metrics/collectors/ready_queue", call_site=_CALLER
    )
    assert not cross_file._candidate_is_under_module_path(
        "src/pkg_b/metrics.py", "", call_site=_CALLER
    )
    assert cross_file._candidate_is_under_module_path(
        "src/pkg_b/metrics.py", "pkg_b/metrics", call_site=_CALLER
    )


@pytest.mark.unit
async def test_a_mirrored_path_outside_the_call_site_root_is_not_evidence() -> None:
    """The imported run must sit under a root the call site itself shares.

    ``tests/pkg_b/observability/execution_platform_metrics.py`` mirrors the
    imported module's segments under an unrelated root, so a module-level
    ``record_ready_queue_depth`` edited there is not reachable from the
    production import and must not satisfy the gate (PRRT_kwDOSJAM6s6q791s).
    """
    mirrored = "tests/pkg_b/observability/execution_platform_metrics.py"
    probe = _Probe(
        texts={(_LEFT, _CALLER): _CALLER_TEXT, (_LEFT, mirrored): _CALLEE_TEXT},
        changed_paths=(mirrored,),
        diffs={mirrored: _IN_SPAN_DIFF.replace(_CALLEE_MODULE, mirrored)},
    )

    assert not await _probe(probe)


@pytest.mark.unit
def test_the_shared_root_may_be_empty_or_the_whole_call_site_package() -> None:
    """Both ends at the repo root, and a nested namespace, still match."""
    assert cross_file._candidate_is_under_module_path(
        "pkg_b/metrics.py", "pkg_b/metrics", call_site="pkg_a/service.py"
    )
    assert cross_file._candidate_is_under_module_path(
        "src/pkg_a/services/pkg_b/metrics.py", "pkg_b/metrics", call_site=_CALLER
    )


@pytest.mark.unit
async def test_non_python_call_sites_keep_the_name_only_rule() -> None:
    """The binding reader only understands Python imports; JS/TS is unchanged."""
    caller_path = "src/pkg_a/services/pools.ts"
    callee_path = "src/pkg_b/metrics.ts"
    caller = (
        "import { helper } from './other';\n"
        "\n"
        "export function refresh(value) {\n"
        "  return helper(value);\n"
        "}\n"
    )
    probe = _Probe(
        texts={
            (_LEFT, caller_path): caller,
            (_LEFT, callee_path): "const helper = (value) => {\n  return value;\n};\n",
        },
        changed_paths=(callee_path,),
        diffs={
            callee_path: (
                f"--- a/{callee_path}\n"
                f"+++ b/{callee_path}\n"
                "@@ -2 +2 @@\n"
                "-  return value;\n"
                "+  return value.depth;\n"
            )
        },
    )

    assert await _probe(probe, item_path=caller_path, item_line=4)


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
    # Imported as a package so every bulk candidate clears the bare-name import
    # binding and the cap stays the only thing bounding the read fan.
    caller = (
        "from pkg_c import record_ready_queue_depth\n"
        "\n"
        "\n"
        "def refresh(payload):\n"
        "    record_ready_queue_depth(payload)\n"
    )
    probe = _Probe(
        texts={
            (_LEFT, _CALLER): caller,
            **{(_LEFT, path): "VALUE = 1\n" for path in bulk},
            (_LEFT, _CALLEE_MODULE): _CALLEE_TEXT,
        },
        changed_paths=(*bulk, _CALLEE_MODULE),
        diffs={_CALLEE_MODULE: _IN_SPAN_DIFF},
    )

    assert not await _probe(probe, item_line=5)
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
