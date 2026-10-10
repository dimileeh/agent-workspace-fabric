"""Cross-file call-site→definition FIXED evidence helpers (issue #1019).

Unit tests for the two probes the correction attempt's fourth evidence gate is
built from: which definitions in *another* file are reachable from module scope,
and whether the item's own commit range changed the definition span of a callee
referenced at the reviewed line.
"""

from __future__ import annotations

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
    ``pkg_b/observability.py``, so a change to ``collector``'s own method there
    is still evidence even though the receiver is not a submodule. The callee
    has to be a member of that imported symbol — see
    ``test_an_imported_object_receiver_rejects_another_class_s_method`` in the
    receiver-binding sibling module (PRRT_kwDOSJAM6s6q-L4H).
    """
    module_file = "src/pkg_b/observability.py"
    caller = (
        "from pkg_b.observability import collector\n"
        "\n"
        "\n"
        "def refresh(payload):\n"
        "    collector.record_ready_queue_depth(payload)\n"
    )
    # ``collector.record_ready_queue_depth`` spans lines 5-7 of its own module.
    module_text = (
        "GAUGE = None\n"
        "\n"
        "\n"
        "class collector:\n"
        "    def record_ready_queue_depth(payload):\n"
        "        GAUGE.set(len(payload.entries))\n"
        "        return None\n"
    )
    probe = _Probe(
        texts={(_LEFT, _CALLER): caller, (_LEFT, module_file): module_text},
        changed_paths=(module_file,),
        diffs={
            module_file: (
                f"--- a/{module_file}\n"
                f"+++ b/{module_file}\n"
                "@@ -6 +6 @@\n"
                "-        GAUGE.set(len(payload.entries))\n"
                "+        GAUGE.set(payload.ready_depth())\n"
            )
        },
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
        "metrics": frozenset({("pkg/obs/metrics", False, None), ("pkg/obs", True, "metrics")}),
        "alt": frozenset({("pkg/other", False, None)}),
    }
    # The aliased form resolves through the *imported* name, not the alias.
    assert cross_file._receiver_import_module_targets(
        "from pkg.obs import metrics as m\n", path="src/pkg_a/caller.py"
    ) == {"m": frozenset({("pkg/obs/metrics", False, None), ("pkg/obs", True, "metrics")})}


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
    # ``pkg`` is the real statement's own package root; the decoy's module path
    # reaches neither binding.
    assert cross_file._plain_import_module_paths(text, path="src/pkg/caller.py") == {
        "real": frozenset({"pkg/real"}),
        "pkg": frozenset({"pkg"}),
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
        "real": frozenset({"pkg/real"}),
        "pkg": frozenset({"pkg"}),
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
        "metrics": frozenset({("pkg/obs", False, None)}),
        "record": frozenset({("pkg/obs", False, None)}),
    }
    assert cross_file._receiver_import_module_targets(text, path="src/pkg_a/caller.py") == {
        "metrics": frozenset({("pkg/obs/metrics", False, None), ("pkg/obs", True, "metrics")}),
        "record": frozenset({("pkg/obs/record", False, None), ("pkg/obs", True, "record")}),
        "obs": frozenset({("pkg/obs", False, None)}),
        "pkg": frozenset({("pkg", False, None)}),
    }


@pytest.mark.unit
def test_a_dotted_plain_import_does_not_rebind_a_same_named_bare_callee() -> None:
    """The receiver reader's last-segment binding is deliberately not a bare one.

    ``import vendor.metrics`` binds ``vendor``; ``_plain_import_module_paths``
    keys it under ``metrics`` because that is the token a *qualified* call site
    writes ahead of the callee (``vendor.metrics.record()``). Feeding that
    receiver-shaped approximation into the bare-name guard as a second binding
    form — the symmetric reading of PRRT_kwDOSJAM6s6q9Xo3, which holds the two
    forms of one path apart for *receivers* — would fail a bare ``metrics(...)``
    closed over a statement that never rebinds it and re-park the #1019 fixes,
    so the bare rule stays a ``from`` import reader.
    """
    text = "from pkg.obs import metrics\nimport vendor.metrics\n"
    assert cross_file._plain_import_module_paths(text, path=_CALLER) == {
        "metrics": frozenset({"vendor/metrics"}),
        "vendor": frozenset({"vendor"}),
    }
    assert cross_file._bare_name_import_module_targets(text, path=_CALLER) == {
        "metrics": frozenset({("pkg/obs", False, None)})
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
