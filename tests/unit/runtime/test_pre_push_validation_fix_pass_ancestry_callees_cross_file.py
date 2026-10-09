"""Cross-file call-site→definition FIXED evidence helpers (issue #1019).

Unit tests for the two probes the correction attempt's fourth evidence gate is
built from: which definitions in *another* file are reachable from module scope,
and whether the item's own commit range changed the definition span of a callee
referenced at the reviewed line.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from awf.runtime.pr_monitor_runner import (
    pre_push_validation_fix_pass_ancestry_cross_file as cross_file,
)
from tests.unit.runtime._pre_push_ancestry_cross_file_helpers import (
    _CALLEE_MODULE,
    _CALLEE_TEXT,
    _CALLER,
    _CALLER_TEXT,
    _IN_SPAN_DIFF,
    _LEFT,
    _OUT_OF_SPAN_DIFF,
    _RIGHT,
    _cross_package_probe,
    _Probe,
    _probe,
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
def test_a_decorator_stack_at_the_top_of_the_file_starts_the_span() -> None:
    """The walk up the stack stops at the first line without running past it."""
    text = "@audit\ndef record_ready_queue_depth(payload):\n    return None\n"
    assert cross_file._importable_definition_spans_for_names(
        text, frozenset({"record_ready_queue_depth"}), path=_CALLEE_MODULE
    ) == [(1, 3)]


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
async def test_a_decorator_added_above_the_callee_is_evidence() -> None:
    """Attaching a decorator to a previously bare callee changes its definition.

    A pure insert is anchored *after* its old-side line, so adding
    ``@retry(...)`` directly above the head reports the line before the span
    start and no hunk overlaps the span (PRRT_kwDOSJAM6s6q791u).
    """
    callee = "GAUGE = None\n\n\ndef record_ready_queue_depth(payload):\n    return GAUGE\n"
    probe = _cross_package_probe(
        callee_text=callee,
        diff=(f"--- a/{_CALLEE_MODULE}\n+++ b/{_CALLEE_MODULE}\n@@ -3,0 +4 @@\n+@retry(times=3)\n"),
    )

    assert await _probe(probe)


@pytest.mark.unit
async def test_a_plain_statement_added_above_the_callee_is_not_evidence() -> None:
    """The insert rule stays decorator-only: unrelated code above the head is not the callee."""
    callee = "GAUGE = None\n\n\ndef record_ready_queue_depth(payload):\n    return GAUGE\n"
    probe = _cross_package_probe(
        callee_text=callee,
        diff=(
            f"--- a/{_CALLEE_MODULE}\n+++ b/{_CALLEE_MODULE}\n@@ -3,0 +4 @@\n+OTHER = compute()\n"
        ),
    )

    assert not await _probe(probe)


@pytest.mark.unit
async def test_a_decorated_helper_inserted_above_the_callee_is_not_evidence() -> None:
    """An inserted definition of its own is a new neighbour, not a change to the callee."""
    callee = "GAUGE = None\n\n\ndef record_ready_queue_depth(payload):\n    return GAUGE\n"
    probe = _cross_package_probe(
        callee_text=callee,
        diff=(
            f"--- a/{_CALLEE_MODULE}\n"
            f"+++ b/{_CALLEE_MODULE}\n"
            "@@ -3,0 +4,3 @@\n"
            "+@audit\n"
            "+def _unrelated_helper():\n"
            "+    return None\n"
        ),
    )

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
async def test_a_comment_on_a_plain_import_binds_no_receiver_of_its_own() -> None:
    """A ``#`` comment ends the statement; words inside it bind no receiver.

    The plain-``import`` reader splits on commas too, so a comma in a trailing
    comment would bind the following word to a module path the call site never
    imported. That receiver then fails closed against every changed file and a
    correct cross-file fix is re-parked as needs_human — the mirror image of the
    ``from``-import comment defect (PRRT_kwDOSJAM6s6q8BmK).
    """
    caller = (
        "import json  # legacy, metrics is injected\n"
        "\n"
        "\n"
        "def refresh(payload, metrics):\n"
        "    metrics.record_ready_queue_depth(payload)\n"
    )

    assert await _probe(_cross_package_probe(caller_text=caller), item_line=5)


@pytest.mark.unit
def test_plain_import_comments_are_stripped_before_the_pieces_are_read() -> None:
    """Only the statement's own dotted names bind; its comment text does not."""
    assert cross_file._plain_import_module_paths(
        "import json  # legacy, metrics is injected\n", path="src/pkg/caller.py"
    ) == {"json": frozenset({"json"})}


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
async def test_a_comment_inside_an_import_block_does_not_drop_later_names() -> None:
    """A ``#`` comment ends at its own line, not at the import block's closer.

    The wrapped target list is joined line by line, so a comment beside one name
    must not swallow the names below it — those would fall back to the name-only
    rule and accept an unrelated same-named definition (PRRT_kwDOSJAM6s6q8BmK).
    """
    caller = (
        "from pkg_c.unrelated import (  # noqa: F401\n"
        "    helper,  # kept for the legacy call path\n"
        "    record_ready_queue_depth,\n"
        ")\n"
        "\n"
        "\n"
        "def refresh(payload):\n"
        "    record_ready_queue_depth(payload)\n"
    )

    assert not await _probe(_cross_package_probe(caller_text=caller), item_line=8)


@pytest.mark.unit
def test_import_comments_are_stripped_per_line_before_the_names_are_read() -> None:
    """Each physical line loses its own comment; the bindings below survive."""
    assert cross_file._bare_name_import_module_paths(
        "from pkg.mod import (  # noqa: F401\n"
        "    alpha,  # a trailing note (with a paren)\n"
        "    beta as gamma,\n"
        ")\n"
        "from pkg.other import delta  # another note\n",
        path="src/pkg/caller.py",
    ) == {
        "alpha": frozenset({"pkg/mod"}),
        "gamma": frozenset({"pkg/mod"}),
        "delta": frozenset({"pkg/other"}),
    }


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
        self.shows: list[tuple[str, str]] = []
        self.texts = {
            (_LEFT, _CALLER): _CALLER_TEXT,
            (_LEFT, _CALLEE_MODULE): _CALLEE_TEXT,
            # The move carries the definition to its rename target, which is
            # where the survival read looks for it (PRRT_kwDOSJAM6s6q8MWy).
            (_RIGHT, _CALLEE_MODULE_RENAMED): _CALLEE_TEXT,
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
    # The callee's survival is read at the rename target, not at the old path.
    assert (_RIGHT, _CALLEE_MODULE_RENAMED) in probe.shows


@pytest.mark.unit
async def test_rename_aware_diff_failure_fails_closed() -> None:
    """An unreadable rename-aware diff is not evidence."""
    probe = _RenameProbe(name_status_z=_RENAME_NAME_STATUS_Z, diffs={})

    assert not await _probe(probe)


# --- the callee must survive the correction (PRRT_kwDOSJAM6s6q8MWy) ----------

# Deleting the callee's whole file: the changed-path set still carries the path,
# its definition spans still read back at ``left``, and the deletion hunk
# overlaps every one of them.
_WHOLE_FILE_DELETION_DIFF = (
    f"diff --git a/{_CALLEE_MODULE} b/{_CALLEE_MODULE}\n"
    "deleted file mode 100644\n"
    f"--- a/{_CALLEE_MODULE}\n"
    "+++ /dev/null\n"
    f"@@ -1,{len(_CALLEE_TEXT.splitlines())} +0,0 @@\n"
    + "".join(f"-{line}\n" for line in _CALLEE_TEXT.splitlines())
)

# Deleting just the callee's definition (old lines 4-7) out of a surviving file.
_DEFINITION_DELETION_DIFF = (
    f"--- a/{_CALLEE_MODULE}\n"
    f"+++ b/{_CALLEE_MODULE}\n"
    "@@ -4,4 +3,0 @@\n"
    "-def record_ready_queue_depth(payload):\n"
    '-    """Record the ready-queue depth."""\n'
    "-    GAUGE.set(len(payload.entries))\n"
    "-    return None\n"
)

_CALLEE_TEXT_WITHOUT_DEFINITION = "GAUGE = None\n\n\ndef unrelated_helper():\n    return 0\n"


@pytest.mark.unit
async def test_a_deleted_callee_file_is_not_evidence() -> None:
    """Deleting the callee's file leaves the unchanged caller calling nothing.

    The overlap alone would resolve the comment as fixed even though the
    reviewed call site now references a missing callee, so the definition has to
    still be reachable on ``right`` (PRRT_kwDOSJAM6s6q8MWy). The status letter is
    deliberately not what the guard reads: the right-side read is the one fact
    that holds for a whole-file deletion and a definition-only deletion alike.
    """
    probe = _Probe(
        texts={
            (_LEFT, _CALLER): _CALLER_TEXT,
            (_LEFT, _CALLEE_MODULE): _CALLEE_TEXT,
            (_RIGHT, _CALLEE_MODULE): None,
        },
        changed_paths=(_CALLEE_MODULE,),
        diffs={_CALLEE_MODULE: _WHOLE_FILE_DELETION_DIFF},
    )

    assert not await _probe(probe)


@pytest.mark.unit
async def test_deleting_only_the_callee_definition_is_not_evidence() -> None:
    """A surviving file that no longer defines the callee is not a fix either."""
    probe = _Probe(
        texts={
            (_LEFT, _CALLER): _CALLER_TEXT,
            (_LEFT, _CALLEE_MODULE): _CALLEE_TEXT,
            (_RIGHT, _CALLEE_MODULE): _CALLEE_TEXT_WITHOUT_DEFINITION,
        },
        changed_paths=(_CALLEE_MODULE,),
        diffs={_CALLEE_MODULE: _DEFINITION_DELETION_DIFF},
    )

    assert not await _probe(probe)


@pytest.mark.unit
async def test_a_callee_definition_moved_inside_its_file_is_still_evidence() -> None:
    """A definition the correction moved within its own file survives on ``right``.

    The old span's deletion hunk overlaps, and the callee is still reachable
    from module scope afterwards, so this stays the #1019 evidence shape.
    """
    moved = (
        "GAUGE = None\n"
        "\n"
        "\n"
        "def unrelated_helper():\n"
        "    return 0\n"
        "\n"
        "\n"
        "def record_ready_queue_depth(payload):\n"
        "    GAUGE.set(payload.ready_depth())\n"
    )
    probe = _Probe(
        texts={
            (_LEFT, _CALLER): _CALLER_TEXT,
            (_LEFT, _CALLEE_MODULE): _CALLEE_TEXT,
            (_RIGHT, _CALLEE_MODULE): moved,
        },
        changed_paths=(_CALLEE_MODULE,),
        diffs={_CALLEE_MODULE: _DEFINITION_DELETION_DIFF},
    )

    assert await _probe(probe)


# The anchored line calls two helpers from the same module, so one candidate
# path carries two callee names.
_TWO_CALLEE_CALLER_TEXT = (
    "from pkg_b.observability.execution_platform_metrics import "
    "record_ready_queue_depth, unrelated_helper\n"
    "\n"
    "\n"
    "def refresh_ready_queue_metrics(pool):\n"
    "    payload = pool.snapshot()\n"
    "    record_ready_queue_depth(payload, unrelated_helper())\n"
    "    return payload\n"
)


@pytest.mark.unit
async def test_a_surviving_sibling_callee_is_not_evidence_for_a_deleted_one() -> None:
    """The callee whose span the range touched is the one that has to survive.

    ``unrelated_helper`` is still defined on ``right`` while
    ``record_ready_queue_depth`` — whose span the deletion hunk overlaps — is
    gone, and the anchored line still calls both. Checking survival against the
    candidate's whole name set would let the sibling vouch for the deletion
    (PRRT_kwDOSJAM6s6q8MWy).
    """
    probe = _Probe(
        texts={
            (_LEFT, _CALLER): _TWO_CALLEE_CALLER_TEXT,
            (_LEFT, _CALLEE_MODULE): _CALLEE_TEXT,
            (_RIGHT, _CALLEE_MODULE): _CALLEE_TEXT_WITHOUT_DEFINITION,
        },
        changed_paths=(_CALLEE_MODULE,),
        diffs={_CALLEE_MODULE: _DEFINITION_DELETION_DIFF},
    )

    assert not await _probe(probe)


@pytest.mark.unit
async def test_one_of_two_callees_changed_in_place_is_still_evidence() -> None:
    """A body edit inside one of two bound callees still resolves per name."""
    probe = _Probe(
        texts={
            (_LEFT, _CALLER): _TWO_CALLEE_CALLER_TEXT,
            (_LEFT, _CALLEE_MODULE): _CALLEE_TEXT,
        },
        changed_paths=(_CALLEE_MODULE,),
        diffs={_CALLEE_MODULE: _IN_SPAN_DIFF},
    )

    assert await _probe(probe)


_RECEIVER_SIBLING = "src/pkg_b/observability/unrelated.py"


@pytest.mark.unit
async def test_receiver_import_rejects_a_sibling_under_its_own_package() -> None:
    """``from pkg import metrics`` binds ``metrics``, not everything under ``pkg``.

    ``metrics.record_ready_queue_depth()`` can only reach the imported object or
    submodule, so a same-named module-level definition edited in a *sibling*
    module of the same package is not evidence that the reviewed call site was
    fixed — resolving the receiver to its containing package would accept it
    (PRRT_kwDOSJAM6s6q8MW9).
    """
    caller = (
        "from pkg_b.observability import execution_platform_metrics as metrics\n"
        "\n"
        "\n"
        "def refresh(payload):\n"
        "    metrics.record_ready_queue_depth(payload)\n"
    )
    probe = _Probe(
        texts={(_LEFT, _CALLER): caller, (_LEFT, _RECEIVER_SIBLING): _CALLEE_TEXT},
        changed_paths=(_RECEIVER_SIBLING,),
        diffs={_RECEIVER_SIBLING: _IN_SPAN_DIFF.replace(_CALLEE_MODULE, _RECEIVER_SIBLING)},
    )

    assert not await _probe(probe, item_line=5)


@pytest.mark.unit
async def test_receiver_import_accepts_the_importing_module_s_own_file() -> None:
    """A receiver may be an object the imported module itself defines.

    ``from pkg_b.observability import collector`` can bind a class declared in
    ``pkg_b/observability.py``, so a change to ``collector``'s method there is
    still evidence even though the receiver is not a submodule.
    """
    module_file = "src/pkg_b/observability.py"
    caller = (
        "from pkg_b.observability import collector\n"
        "\n"
        "\n"
        "def refresh(payload):\n"
        "    collector.record_ready_queue_depth(payload)\n"
    )
    probe = _Probe(
        texts={(_LEFT, _CALLER): caller, (_LEFT, module_file): _CALLEE_TEXT},
        changed_paths=(module_file,),
        diffs={module_file: _IN_SPAN_DIFF.replace(_CALLEE_MODULE, module_file)},
    )

    assert await _probe(probe, item_line=5)


@pytest.mark.unit
def test_receiver_targets_keep_the_imported_name_and_pin_its_package() -> None:
    """``from M import R`` offers ``M/R`` with descendants, plus ``M`` exactly."""
    # ``True`` pins the target to the module's own file; ``False`` keeps the
    # package-re-export tolerance (see ``_ModuleTarget``).
    assert cross_file._receiver_import_module_targets(
        "from pkg.obs import metrics\nimport pkg.other as alt\n", path="src/pkg_a/caller.py"
    ) == {
        "metrics": frozenset({("pkg/obs/metrics", False), ("pkg/obs", True)}),
        "alt": frozenset({("pkg/other", False)}),
    }
    # The aliased form resolves through the *imported* name, not the alias.
    assert cross_file._receiver_import_module_targets(
        "from pkg.obs import metrics as m\n", path="src/pkg_a/caller.py"
    ) == {"m": frozenset({("pkg/obs/metrics", False), ("pkg/obs", True)})}


@pytest.mark.unit
def test_an_exact_module_target_matches_only_that_module_s_own_file() -> None:
    """``exact`` accepts ``M.py`` and ``M/__init__.py`` but no sibling under ``M``."""
    for candidate in ("src/pkg_b/obs.py", "src/pkg_b/obs/__init__.py"):
        assert cross_file._candidate_is_under_module_path(
            candidate, "pkg_b/obs", call_site=_CALLER, exact=True
        )
    assert not cross_file._candidate_is_under_module_path(
        "src/pkg_b/obs/unrelated.py", "pkg_b/obs", call_site=_CALLER, exact=True
    )
    # Without ``exact`` the same sibling stays in scope for a bare callee.
    assert cross_file._candidate_is_under_module_path(
        "src/pkg_b/obs/unrelated.py", "pkg_b/obs", call_site=_CALLER
    )


@pytest.mark.unit
async def test_an_import_quoted_in_a_docstring_binds_no_decoy_module() -> None:
    """A docstring example's import must not union a decoy module in.

    The caller really imports the callee from ``pkg_c.unrelated``; the usage
    example in its docstring names ``pkg_b.observability``. A lexical scan would
    union both, and since any stored path satisfies the candidate match, editing
    the *example's* module would read as a fix of a call site that still routes
    to ``pkg_c`` (PRRT_kwDOSJAM6s6q8MXB).
    """
    caller = (
        "from pkg_c.unrelated import record_ready_queue_depth\n"
        "\n"
        "\n"
        "def refresh(payload):\n"
        '    """Refresh the gauge.\n'
        "\n"
        "    Example:\n"
        "        from pkg_b.observability import record_ready_queue_depth\n"
        '    """\n'
        "    record_ready_queue_depth(payload)\n"
    )

    assert not await _probe(_cross_package_probe(caller_text=caller), item_line=10)


@pytest.mark.unit
def test_import_readers_skip_heads_inside_string_literals() -> None:
    """Both readers run over the comment/string-masked scan lines."""
    text = (
        "from pkg.mod import alpha\n"
        "import pkg.real\n"
        'DOC = """\n'
        "from pkg.decoy import alpha\n"
        "import pkg.decoy\n"
        '"""\n'
    )
    assert cross_file._bare_name_import_module_paths(text, path="src/pkg/caller.py") == {
        "alpha": frozenset({"pkg/mod"})
    }
    assert cross_file._plain_import_module_paths(text, path="src/pkg/caller.py") == {
        "real": frozenset({"pkg/real"})
    }


@pytest.mark.unit
def test_import_readers_skip_heads_inside_retained_interpolations() -> None:
    """A head left readable by interpolation retention still binds nothing.

    The masked scan keeps f-string ``{...}`` bodies scannable so interpolated
    calls stay visible, which leaves an import head quoted inside a multi-line
    interpolation looking like code. It is still inside the interpolation's
    brace, and an import head never sits inside an open bracket, so the decoy
    module must not reach either binding (PRRT_kwDOSJAM6s6q8MXB).
    """
    text = (
        "from pkg.mod import alpha\n"
        "import pkg.real\n"
        'DOC = f"""{\n'
        "from pkg.decoy import alpha\n"
        "import pkg.decoy\n"
        '}"""\n'
    )
    assert cross_file._bare_name_import_module_paths(text, path="src/pkg/caller.py") == {
        "alpha": frozenset({"pkg/mod"})
    }
    assert cross_file._plain_import_module_paths(text, path="src/pkg/caller.py") == {
        "real": frozenset({"pkg/real"})
    }


@pytest.mark.unit
def test_a_parenthesized_import_head_still_binds_every_wrapped_name() -> None:
    """Holding heads to bracket depth 0 keeps the wrapped target list readable."""
    text = "from pkg.mod import (\n    alpha,\n    beta,\n)\n"
    assert cross_file._bare_name_import_module_paths(text, path="src/pkg/caller.py") == {
        "alpha": frozenset({"pkg/mod"}),
        "beta": frozenset({"pkg/mod"}),
    }


@pytest.mark.unit
async def test_a_rebound_bare_callee_fails_closed_against_both_modules() -> None:
    """A re-imported bare name resolves to one module, so neither is evidence.

    ``from pkg_b... import record_ready_queue_depth`` followed by ``from
    pkg_c.unrelated import record_ready_queue_depth`` leaves the call bound to
    the second module only. Unioning both paths would accept a correction to the
    *shadowed* ``pkg_b`` definition while the runtime callee in ``pkg_c`` stays
    unchanged, so an ambiguous binding fails closed (PRRT_kwDOSJAM6s6q8-Mw).
    """
    caller = (
        "from pkg_b.observability.execution_platform_metrics import record_ready_queue_depth\n"
        "from pkg_c.unrelated import record_ready_queue_depth\n"
        "\n"
        "\n"
        "def refresh(payload):\n"
        "    record_ready_queue_depth(payload)\n"
    )

    assert cross_file._bare_name_import_module_targets(caller, path=_CALLER) == {
        "record_ready_queue_depth": cross_file._AMBIGUOUS_IMPORT_TARGET
    }
    assert not await _probe(_cross_package_probe(caller_text=caller), item_line=6)


@pytest.mark.unit
async def test_a_rebound_receiver_fails_closed_against_both_modules() -> None:
    """A receiver bound twice reaches one module, so no changed path matches.

    The ``from``/plain mix is the same defect through the receiver key: the call
    goes through the last binding, so accepting the first module's edit would
    resolve a thread whose runtime callee is untouched (PRRT_kwDOSJAM6s6q8-Mw).
    """
    module_file = "src/pkg_b/observability.py"
    caller = (
        "from pkg_b.observability import collector\n"
        "import pkg_c.collector as collector\n"
        "\n"
        "\n"
        "def refresh(payload):\n"
        "    collector.record_ready_queue_depth(payload)\n"
    )
    probe = _Probe(
        texts={(_LEFT, _CALLER): caller, (_LEFT, module_file): _CALLEE_TEXT},
        changed_paths=(module_file,),
        diffs={module_file: _IN_SPAN_DIFF.replace(_CALLEE_MODULE, module_file)},
    )

    assert cross_file._receiver_import_module_targets(caller, path=_CALLER) == {
        "collector": cross_file._AMBIGUOUS_IMPORT_TARGET
    }
    assert not await _probe(probe, item_line=6)


@pytest.mark.unit
def test_an_import_repeated_for_the_same_module_still_binds_it() -> None:
    """Only a *rebinding* is ambiguous; the same target twice keeps its path.

    A name imported from one module under both ``TYPE_CHECKING`` and the runtime
    branch binds that module either way, so it must not fail closed.
    """
    text = (
        "from pkg.obs import metrics\n"
        "from pkg.obs import metrics\n"
        "from pkg.obs import record\n"
        "import pkg.obs\n"
        "import pkg.obs as obs\n"
    )
    assert cross_file._bare_name_import_module_targets(text, path="src/pkg_a/caller.py") == {
        "metrics": frozenset({("pkg/obs", False)}),
        "record": frozenset({("pkg/obs", False)}),
    }
    assert cross_file._receiver_import_module_targets(text, path="src/pkg_a/caller.py") == {
        "metrics": frozenset({("pkg/obs/metrics", False), ("pkg/obs", True)}),
        "record": frozenset({("pkg/obs/record", False), ("pkg/obs", True)}),
        "obs": frozenset({("pkg/obs", False)}),
    }


@pytest.mark.unit
async def test_a_bare_callee_realiased_within_one_module_fails_closed() -> None:
    """Rebinding inside one module is still a rebinding, so it fails closed.

    ``from M import record_ready_queue_depth`` followed by ``from M import
    unrelated_helper as record_ready_queue_depth`` leaves the call reaching
    ``M``'s ``unrelated_helper``. Judging the rebinding on the module alone
    would call this one binding and accept a correction to the shadowed
    same-named definition under ``M`` as evidence about a callee the range never
    touched, exactly as unioning two modules did (PRRT_kwDOSJAM6s6q8-Mw).
    """
    import_head = "from pkg_b.observability.execution_platform_metrics import "
    caller = (
        f"{import_head}record_ready_queue_depth\n"
        f"{import_head}unrelated_helper as record_ready_queue_depth\n"
        "\n"
        "\n"
        "def refresh(payload):\n"
        "    record_ready_queue_depth(payload)\n"
    )

    assert cross_file._bare_name_import_module_targets(caller, path=_CALLER) == {
        "record_ready_queue_depth": cross_file._AMBIGUOUS_IMPORT_TARGET
    }
    assert not await _probe(_cross_package_probe(caller_text=caller), item_line=6)


@pytest.mark.unit
def test_an_aliased_bare_import_keeps_the_imported_symbols_name() -> None:
    """``import actual as alias`` binds ``alias`` but names ``actual``.

    The local name narrows the candidate path; the definition to look for inside
    it is the imported symbol (PRRT_kwDOSJAM6s6q9WnP). An unaliased import, and
    a name two imports bind to different symbols, carry no rename.
    """
    text = (
        "from pkg.mod import actual as alias\n"
        "from pkg.mod import plain\n"
        "from pkg.mod import first as rebound\n"
        "from pkg.other import second as rebound\n"
    )
    assert cross_file._bare_name_imported_definition_names(text, path=_CALLER) == {
        "alias": "actual"
    }


@pytest.mark.unit
async def test_an_aliased_bare_callee_resolves_to_the_imported_definition() -> None:
    """A correction to the aliased callee's real definition is evidence.

    ``from M import record_ready_queue_depth as record`` reaches ``M``'s
    ``record_ready_queue_depth``, so searching ``M`` for ``def record`` would
    reject the fix that edits it (PRRT_kwDOSJAM6s6q9WnP).
    """
    caller = (
        "from pkg_b.observability.execution_platform_metrics import "
        "record_ready_queue_depth as record\n"
        "\n"
        "\n"
        "def refresh(payload):\n"
        "    record(payload)\n"
    )

    assert await _probe(_cross_package_probe(caller_text=caller), item_line=5)


@pytest.mark.unit
async def test_an_alias_colliding_with_a_sibling_definition_fails_closed() -> None:
    """An edit to the module's own ``unrelated_helper`` is not evidence.

    The call site aliases ``record_ready_queue_depth`` to ``unrelated_helper``,
    a name the same module also defines. Looking the local binding up in the
    imported module would accept a correction to that sibling as proof the
    reviewed call was fixed (PRRT_kwDOSJAM6s6q9WnP).
    """
    caller = (
        "from pkg_b.observability.execution_platform_metrics import "
        "record_ready_queue_depth as unrelated_helper\n"
        "\n"
        "\n"
        "def refresh(payload):\n"
        "    unrelated_helper(payload)\n"
    )
    probe = _cross_package_probe(caller_text=caller, diff=_OUT_OF_SPAN_DIFF)

    assert not await _probe(probe, item_line=5)
