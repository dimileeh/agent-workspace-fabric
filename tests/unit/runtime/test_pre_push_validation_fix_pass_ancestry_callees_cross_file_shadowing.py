"""Local-shadowing rules for cross-file callee evidence (issue #1019).

Unit tests for the one thing an import binding cannot prove on its own: that
the name still *holds* that import where the review is anchored. A parameter,
a local assignment, a nested definition or a module-level reassignment of the
same name leaves the call reaching that binding instead, so the imported
module's same-named definition is not the callee a correction has to touch
(PRRT_kwDOSJAM6s6q9WnX). Kept beside
``test_pre_push_validation_fix_pass_ancestry_callees_cross_file.py`` so both
stay under the first-party file line limit.
"""

from __future__ import annotations

import pytest

from awf.runtime.pr_monitor_runner import (
    pre_push_validation_fix_pass_ancestry_cross_file as cross_file,
)
from awf.runtime.pr_monitor_runner import (
    pre_push_validation_fix_pass_ancestry_rebinding as rebinding,
)
from tests.unit.runtime._pre_push_ancestry_cross_file_helpers import (
    _CALLEE_MODULE,
    _CALLEE_TEXT,
    _CALLER,
    _IN_SPAN_DIFF,
    _LEFT,
    _cross_package_probe,
    _Probe,
    _probe,
)

_IMPORT_LINE = (
    "from pkg_b.observability.execution_platform_metrics import record_ready_queue_depth\n"
)

# The imported bare callee is also the enclosing function's parameter, so the
# call on line 6 reaches the parameter, not the imported module's definition.
_PARAMETER_SHADOW_TEXT = (
    f"{_IMPORT_LINE}"
    "\n"
    "\n"
    "def refresh(record_ready_queue_depth, pool):\n"
    "    payload = pool.snapshot()\n"
    "    record_ready_queue_depth(payload)\n"
)

# The same shape through a local assignment instead of a parameter.
_ASSIGNMENT_SHADOW_TEXT = (
    f"{_IMPORT_LINE}"
    "\n"
    "\n"
    "def refresh(pool):\n"
    "    record_ready_queue_depth = pool.recorder\n"
    "    record_ready_queue_depth(pool.snapshot())\n"
)

# A receiver a plain ``import`` would otherwise prove to be that module, bound
# at the anchored scope to whatever the caller passes in.
_RECEIVER_SHADOW_TEXT = (
    "import pkg_b.observability.execution_platform_metrics as metrics\n"
    "\n"
    "\n"
    "def refresh(metrics, payload):\n"
    "    metrics.record_ready_queue_depth(payload)\n"
)

_RECEIVER_IMPORT_TEXT = (
    "import pkg_b.observability.execution_platform_metrics as metrics\n"
    "\n"
    "\n"
    "def refresh(payload):\n"
    "    metrics.record_ready_queue_depth(payload)\n"
)


def _receiver_probe(*, caller_text: str) -> _Probe:
    return _Probe(
        texts={
            (_LEFT, _CALLER): caller_text,
            (_LEFT, _CALLEE_MODULE): _CALLEE_TEXT,
        },
        changed_paths=(_CALLEE_MODULE,),
        diffs={_CALLEE_MODULE: _IN_SPAN_DIFF},
    )


@pytest.mark.unit
async def test_a_parameter_shadowing_the_import_fails_the_bare_callee_closed() -> None:
    """``from pkg.mod import f`` proves nothing about ``def run(f): f()``.

    The correction edits ``record_ready_queue_depth`` in the imported module,
    but the anchored line calls the parameter, so the callee it really reaches
    is untouched and the item must not resolve on this evidence
    (PRRT_kwDOSJAM6s6q9WnX).
    """
    probe = _cross_package_probe(caller_text=_PARAMETER_SHADOW_TEXT)

    assert not await _probe(probe, item_line=6)


@pytest.mark.unit
async def test_a_local_assignment_shadowing_the_import_fails_closed() -> None:
    """A local assignment binds the name function-wide, so the import is not held."""
    probe = _cross_package_probe(caller_text=_ASSIGNMENT_SHADOW_TEXT)

    assert not await _probe(probe, item_line=6)


# The callee's only binding is a *function-local* import — the lazy-import
# shape — so the call on line 5 reaches the imported definition and a
# correction confined to it is evidence about this call site.
_LOCAL_IMPORT_TEXT = (
    "def refresh(pool):\n"
    f"    {_IMPORT_LINE}"
    "\n"
    "    payload = pool.snapshot()\n"
    "    record_ready_queue_depth(payload)\n"
)

# Two *sibling* functions lazily importing the same local name from different
# modules. Neither import is visible in the other's body, so the call on line 5
# reaches ``pkg_b``'s definition and the one on line 11 reaches ``pkg_c``'s.
_SIBLING_LOCAL_IMPORT_TEXT = (
    "def refresh(pool):\n"
    f"    {_IMPORT_LINE}"
    "\n"
    "    payload = pool.snapshot()\n"
    "    record_ready_queue_depth(payload)\n"
    "\n"
    "\n"
    "def legacy(pool):\n"
    "    from pkg_c.legacy.metrics import record_ready_queue_depth\n"
    "\n"
    "    record_ready_queue_depth(pool)\n"
)

# The receiver form of the same shape: two sibling functions each importing a
# different module under the same local receiver name.
_SIBLING_LOCAL_PLAIN_IMPORT_TEXT = (
    "def refresh(payload):\n"
    "    import pkg_b.observability.execution_platform_metrics as metrics\n"
    "\n"
    "    metrics.record_ready_queue_depth(payload)\n"
    "\n"
    "\n"
    "def legacy(payload):\n"
    "    import pkg_c.legacy.metrics as metrics\n"
    "\n"
    "    metrics.record_ready_queue_depth(payload)\n"
)

# A local import of the *same name* from another module rebinds it, so the
# readers cannot tell which statement runs last and the name fails closed.
_LOCAL_IMPORT_REBIND_TEXT = (
    f"{_IMPORT_LINE}"
    "\n"
    "\n"
    "def refresh(pool):\n"
    "    from pkg_c.legacy.metrics import record_ready_queue_depth\n"
    "\n"
    "    payload = pool.snapshot()\n"
    "    record_ready_queue_depth(payload)\n"
)


@pytest.mark.unit
async def test_a_function_local_import_still_supplies_its_own_evidence() -> None:
    """A lazy import is the call's binding, not a shadow of it.

    ``def run(): from pkg_b.mod import validate; validate()`` reaches the
    imported definition, so a correction confined to it in another package is
    the #1019 evidence this gate exists to accept — reading the import as a
    local rebinding parked such a fix as ``needs_human``
    (PRRT_kwDOSJAM6s6rAhm2).
    """
    probe = _cross_package_probe(caller_text=_LOCAL_IMPORT_TEXT)

    assert await _probe(probe, item_line=5)


@pytest.mark.unit
async def test_a_function_local_import_rebinding_the_module_import_fails_closed() -> None:
    """Two imports of one name stay unmatchable, local or not.

    The lexical readers cannot order a function-local import against the
    module-level one, so the name is held to ``_AMBIGUOUS_IMPORT_TARGET``
    rather than crediting a correction to the shadowed definition.
    """
    probe = _cross_package_probe(caller_text=_LOCAL_IMPORT_REBIND_TEXT)

    assert not await _probe(probe, item_line=8)


@pytest.mark.unit
async def test_a_sibling_functions_lazy_import_leaves_the_name_unambiguous() -> None:
    """A lazy import in another function is invisible at the anchored line.

    ``def a(): from pkg.real import record`` and ``def b(): from pkg.decoy
    import record`` bind two *locals*, not one name twice, so reading the file's
    import heads as a single identity set marked ``record`` ambiguous at both
    call sites and parked a legitimate cross-file correction as ``needs_human``
    (PRRT_kwDOSJAM6s6rA-ru).
    """
    probe = _cross_package_probe(caller_text=_SIBLING_LOCAL_IMPORT_TEXT)

    assert await _probe(probe, item_line=5)


@pytest.mark.unit
def test_import_targets_are_read_only_from_the_scopes_holding_the_line() -> None:
    """Each sibling's lazy import binds the name at its own call site only."""
    targets = cross_file._bare_name_import_module_targets(
        _SIBLING_LOCAL_IMPORT_TEXT, path=_CALLER, line=5
    )

    assert targets == {
        "record_ready_queue_depth": frozenset(
            {("pkg_b/observability/execution_platform_metrics", False, None)}
        )
    }
    assert cross_file._bare_name_import_module_targets(
        _SIBLING_LOCAL_IMPORT_TEXT, path=_CALLER, line=11
    ) == {"record_ready_queue_depth": frozenset({("pkg_c/legacy/metrics", False, None)})}
    # With no anchored line the readers keep the file-wide — conservative —
    # reading, which is the rebinding both imports look like together.
    assert cross_file._bare_name_import_module_targets(
        _SIBLING_LOCAL_IMPORT_TEXT, path=_CALLER
    ) == {"record_ready_queue_depth": cross_file._AMBIGUOUS_IMPORT_TARGET}


@pytest.mark.unit
def test_plain_import_receivers_are_read_only_from_the_scopes_holding_the_line() -> None:
    """The receiver form is scoped the same way, including its proven-module read."""
    assert cross_file._plain_import_module_paths(
        _SIBLING_LOCAL_PLAIN_IMPORT_TEXT, path=_CALLER, line=4
    ) == {"metrics": frozenset({"pkg_b/observability/execution_platform_metrics"})}
    assert cross_file._receiver_import_module_targets(
        _SIBLING_LOCAL_PLAIN_IMPORT_TEXT, path=_CALLER, line=10
    ) == {"metrics": frozenset({("pkg_c/legacy/metrics", False, None)})}
    assert cross_file._module_bound_receiver_names(
        _SIBLING_LOCAL_PLAIN_IMPORT_TEXT, path=_CALLER, line=4
    ) == frozenset({"metrics"})


@pytest.mark.unit
async def test_a_sibling_functions_lazy_receiver_import_still_resolves() -> None:
    """The receiver shape of the sibling-scoping accept, end to end."""
    probe = _receiver_probe(caller_text=_SIBLING_LOCAL_PLAIN_IMPORT_TEXT)

    assert await _probe(probe, item_line=4)


@pytest.mark.unit
async def test_an_unshadowed_bare_callee_still_resolves() -> None:
    """The paired accept: the #1019 shape keeps resolving across the two files."""
    assert await _probe(_cross_package_probe())


@pytest.mark.unit
async def test_a_parameter_shadowing_a_module_receiver_fails_closed() -> None:
    """A shadowed receiver is whatever the caller passes, not the imported module."""
    assert not await _probe(_receiver_probe(caller_text=_RECEIVER_SHADOW_TEXT), item_line=5)


@pytest.mark.unit
async def test_an_unshadowed_module_receiver_still_resolves() -> None:
    """The paired accept for the receiver shape."""
    assert await _probe(_receiver_probe(caller_text=_RECEIVER_IMPORT_TEXT), item_line=5)


@pytest.mark.unit
def test_parameters_and_locals_of_the_anchored_scope_are_read() -> None:
    """Both binding forms are read, and only from a scope that encloses the line."""
    text = (
        "def refresh(param):\n"
        "    assigned = param\n"
        "    return assigned\n"
        "\n"
        "\n"
        "def other(elsewhere):\n"
        "    return elsewhere\n"
    )

    assert cross_file._locally_rebound_names_at_line(text, 2, path=_CALLER) == frozenset(
        {"param", "assigned"}
    )
    assert cross_file._locally_rebound_names_at_line(text, 7, path=_CALLER) == frozenset(
        {"elsewhere"}
    )


@pytest.mark.unit
def test_loop_and_context_targets_of_the_anchored_scope_are_read_but_imports_are_not() -> None:
    """Loop, ``with`` and ``except`` targets bind the name; a local import does not.

    A function-local import is the binding the import readers themselves read,
    so reporting it here would invalidate its own evidence and park a
    correction confined to the module it names (PRRT_kwDOSJAM6s6rAhm2).
    """
    text = (
        "def refresh(paths):\n"
        "    for looped in paths:\n"
        "        with open(looped) as opened:\n"
        "            try:\n"
        "                import pkg.late as late\n"
        "                from pkg import imported\n"
        "            except ValueError as caught:\n"
        "                return caught, late, imported, opened\n"
        "    return None\n"
    )

    assert cross_file._locally_rebound_names_at_line(text, 8, path=_CALLER) == frozenset(
        {"paths", "looped", "opened", "caught"}
    )


@pytest.mark.unit
def test_a_name_both_locally_imported_and_assigned_is_still_read() -> None:
    """The import exemption is per *binding*, not per name.

    A scope that also assigns the name — or takes it as a parameter — reaches
    that binding rather than its own import, which the lexical import readers
    cannot see, so it keeps failing closed (PRRT_kwDOSJAM6s6q9WnX).
    """
    text = (
        "def refresh(flag):\n"
        "    from pkg import validate\n"
        "    if flag:\n"
        "        validate = flag.validator\n"
        "    return validate()\n"
    )

    assert cross_file._locally_rebound_names_at_line(text, 5, path=_CALLER) == frozenset(
        {"flag", "validate"}
    )


@pytest.mark.unit
def test_match_case_capture_targets_of_the_anchored_scope_are_read() -> None:
    """A ``case`` capture binds the name without ever storing an ``ast.Name``.

    Capture, star and mapping-rest targets are carried on the pattern nodes
    themselves, so a reader that only looks for ``Store`` names would leave a
    ``case record:`` holding its import and let a correction to the imported
    definition satisfy a call that actually reaches the capture
    (PRRT_kwDOSJAM6s6q-N1B). The wildcard ``_`` binds nothing and is not
    reported.
    """
    text = (
        "def refresh(event):\n"
        "    match event:\n"
        "        case [captured, *starred]:\n"
        "            return captured, starred\n"
        '        case {"kind": 1, **rest}:\n'
        "            return rest\n"
        "        case [1] as aliased:\n"
        "            return aliased\n"
        "        case _:\n"
        "            return None\n"
    )

    assert cross_file._locally_rebound_names_at_line(text, 10, path=_CALLER) == frozenset(
        {"event", "captured", "starred", "rest", "aliased"}
    )


@pytest.mark.unit
def test_a_nested_definition_name_shadows_but_the_scope_s_own_name_does_not() -> None:
    """A nested ``def`` / ``class`` binds its name in the scope that holds it.

    The enclosing definition's own name is bound in *its* enclosing scope, not
    inside it, so a method named like an imported helper does not shadow that
    import for the calls in its own body.
    """
    text = (
        "class Collector:\n"
        "    def record(self):\n"
        "        def helper():\n"
        "            return 1\n"
        "\n"
        "        class Inner:\n"
        "            pass\n"
        "\n"
        "        return helper(), Inner\n"
    )

    assert cross_file._locally_rebound_names_at_line(text, 9, path=_CALLER) == frozenset(
        {"self", "helper", "Inner"}
    )


@pytest.mark.unit
def test_a_lambda_parameter_shadows_at_its_own_line() -> None:
    """A lambda opens a function scope too, so its parameter shadows the import."""
    text = "handler = lambda record: record()\n"

    assert cross_file._locally_rebound_names_at_line(text, 1, path=_CALLER) == frozenset({"record"})


@pytest.mark.unit
def test_a_class_body_binding_is_not_a_local_shadow() -> None:
    """A class attribute is invisible to the calls inside the class's methods."""
    text = (
        "class Collector:\n    record = None\n\n    def refresh(self):\n        return record()\n"
    )

    assert cross_file._locally_rebound_names_at_line(text, 5, path=_CALLER) == frozenset({"self"})


@pytest.mark.unit
def test_unreadable_call_sites_report_no_local_bindings() -> None:
    """A non-Python path and unparseable text both leave the bindings untouched."""
    text = "def refresh(record):\n    return record()\n"

    assert cross_file._locally_rebound_names_at_line(text, 2, path="src/web/app.ts") == frozenset()
    assert (
        cross_file._locally_rebound_names_at_line("def refresh(record:\n", 1, path=_CALLER)
        == frozenset()
    )


@pytest.mark.unit
def test_a_rebound_name_is_held_to_the_unmatchable_target() -> None:
    """Fail closed rather than widening a shadowed name back to the name-only rule."""
    bindings = {
        "record": frozenset({("src/pkg_b/metrics", False)}),
        "other": frozenset({("m", True)}),
    }

    held = cross_file._import_targets_without_locally_rebound(bindings, frozenset({"record"}))

    assert held["record"] == cross_file._AMBIGUOUS_IMPORT_TARGET
    assert held["other"] == bindings["other"]


# The imported bare callee is reassigned at *module* level, so the call on
# line 8 reaches that module global rather than the imported definition — and a
# module global is visible inside the function too.
_MODULE_ASSIGNMENT_SHADOW_TEXT = (
    f"{_IMPORT_LINE}"
    "\n"
    "record_ready_queue_depth = build_recorder()\n"
    "\n"
    "\n"
    "def refresh(pool):\n"
    "    payload = pool.snapshot()\n"
    "    record_ready_queue_depth(payload)\n"
)

# The same shape for a receiver a plain ``import`` would otherwise prove to be
# the imported module.
_MODULE_RECEIVER_SHADOW_TEXT = (
    "import pkg_b.observability.execution_platform_metrics as metrics\n"
    "\n"
    "metrics = Collector()\n"
    "\n"
    "\n"
    "def refresh(payload):\n"
    "    metrics.record_ready_queue_depth(payload)\n"
)


@pytest.mark.unit
async def test_a_module_level_assignment_shadowing_the_import_fails_closed() -> None:
    """A module global rebinding the import is not the callee either.

    ``from pkg.mod import f`` followed by ``f = build()`` leaves every later
    call — at module level or inside a function that reads the global —
    reaching the reassigned global, so editing ``pkg/mod.py``'s ``f`` changes
    nothing the anchored line calls (PRRT_kwDOSJAM6s6q9WnX).
    """
    probe = _cross_package_probe(caller_text=_MODULE_ASSIGNMENT_SHADOW_TEXT)

    assert not await _probe(probe, item_line=8)


@pytest.mark.unit
async def test_a_module_level_assignment_shadowing_a_receiver_fails_closed() -> None:
    """The receiver form of the module-global rebinding."""
    probe = _receiver_probe(caller_text=_MODULE_RECEIVER_SHADOW_TEXT)

    assert not await _probe(probe, item_line=7)


# The module body defines the callee *and* the anchored function imports it
# lazily: the local import makes the name local to ``refresh`` for the whole
# body, so the module-level ``def`` is unreachable from the call on line 9.
_LOCAL_IMPORT_OVER_MODULE_DEF_TEXT = (
    "def record_ready_queue_depth(payload):\n"
    "    return None\n"
    "\n"
    "\n"
    "def refresh(pool):\n"
    f"    {_IMPORT_LINE}"
    "\n"
    "    payload = pool.snapshot()\n"
    "    record_ready_queue_depth(payload)\n"
)

# The import sits in a *class* body instead, which has no function-wide local
# rule, so a call executing directly in that body before the import line still
# reaches the module-level definition and has to keep failing closed.
_CLASS_BODY_IMPORT_OVER_MODULE_DEF_TEXT = (
    "def record_ready_queue_depth(payload):\n"
    "    return None\n"
    "\n"
    "\n"
    "class Refresher:\n"
    f"    {_IMPORT_LINE}"
    "\n"
    "    depth = record_ready_queue_depth(0)\n"
)


@pytest.mark.unit
async def test_a_lazy_import_outranks_a_module_level_definition() -> None:
    """A function-local import is not invalidated by the module's own binding.

    ``def record(...)`` at module level followed by ``def refresh(): from
    pkg_b... import record; record()`` calls the *imported* definition — the
    local import binds the name for the whole of ``refresh`` — so a correction
    confined to that definition in another package is evidence about this call
    site and must not park as ``needs_human`` (PRRT_kwDOSJAM6s6rAhm2).
    """
    probe = _cross_package_probe(caller_text=_LOCAL_IMPORT_OVER_MODULE_DEF_TEXT)

    assert await _probe(probe, item_line=9)


@pytest.mark.unit
async def test_a_class_body_import_does_not_outrank_a_module_level_definition() -> None:
    """Only a *function* scope makes its import local for the whole body."""
    probe = _cross_package_probe(caller_text=_CLASS_BODY_IMPORT_OVER_MODULE_DEF_TEXT)

    assert not await _probe(probe, item_line=8)


@pytest.mark.unit
def test_hidden_import_lines_fail_open_to_the_file_wide_reading() -> None:
    """Unparseable text hides nothing, which keeps the conservative reading."""
    assert rebinding._import_lines_hidden_from("def refresh(\n", 1) == frozenset()
    # A class body carries no function-wide local rule, so its import lines
    # stay visible to every anchor.
    assert rebinding._import_lines_hidden_from(f"class C:\n    {_IMPORT_LINE}", 1) == frozenset()


@pytest.mark.unit
def test_function_local_import_names_are_read_for_the_enclosing_scopes() -> None:
    """Every function scope holding the line contributes its own import names.

    A nested function's import reaches the lines inside it, an enclosing one's
    reaches them too, and a sibling scope's does not. A class body's import is
    an attribute rather than a local, and a name the scope declares ``global``
    is written back to the module binding instead of bound locally, so neither
    is reported.
    """
    text = (
        "def outer():\n"
        "    import outer_bound\n"
        "\n"
        "    class Holder:\n"
        "        import class_bound\n"
        "\n"
        "    def inner():\n"
        "        global declared\n"
        "        import inner_bound, declared\n"
        "        from pkg.mod import aliased as renamed\n"
        "        return inner_bound\n"
        "\n"
        "\n"
        "def sibling():\n"
        "    import sibling_bound\n"
    )

    assert rebinding._function_local_import_names_at_line(text, 11) == frozenset(
        {"outer_bound", "inner_bound", "renamed"}
    )


@pytest.mark.unit
def test_unparsable_text_reports_no_function_local_imports() -> None:
    """A reader that cannot parse leaves the module-scope verdict untouched."""
    assert rebinding._function_local_import_names_at_line("def f(\n", 1) == frozenset()


@pytest.mark.unit
def test_module_scope_rebindings_are_read_but_import_aliases_are_not() -> None:
    """Every module-scope binding form counts, except the imports themselves.

    An ``import`` statement *is* the binding this reader exists to trust, so its
    own aliases are not reported; a conditional or loop body at module level is
    still module scope, and neither a function body nor a lambda's parameters
    are.
    """
    text = (
        "import pkg.late as late\n"
        "from pkg import imported\n"
        "assigned = 1\n"
        "annotated: int = 2\n"
        "assigned += 1\n"
        "for looped in assigned:\n"
        "    with open(looped) as opened:\n"
        "        pass\n"
        "try:\n"
        "    pass\n"
        "except ValueError as caught:\n"
        "    pass\n"
        "if (walrus := assigned):\n"
        "    pass\n"
        "handler = lambda lambda_param: lambda_param\n"
        "\n"
        "\n"
        "def refresh():\n"
        "    local_only = 1\n"
        "    return local_only\n"
        "\n"
        "\n"
        "class Collector:\n"
        "    attribute = None\n"
    )

    assert cross_file._module_scope_rebound_names(text, path=_CALLER) == frozenset(
        {
            "assigned",
            "annotated",
            "looped",
            "opened",
            "caught",
            "walrus",
            "handler",
            "refresh",
            "Collector",
        }
    )


@pytest.mark.unit
def test_module_scope_match_case_captures_are_read() -> None:
    """A module-level ``case`` capture rebinds the import just as an assignment does.

    ``match`` at module scope is module scope, and its capture targets are
    pattern-node names rather than ``Store`` names, so they have to be read
    explicitly or the rebinding goes unnoticed (PRRT_kwDOSJAM6s6q-N1B).
    """
    text = (
        "from pkg import record\n"
        "\n"
        "match SETTINGS:\n"
        "    case [record, *tail]:\n"
        "        pass\n"
        '    case {"kind": 1, **leftover}:\n'
        "        pass\n"
        "    case {} as whole:\n"
        "        pass\n"
        "    case _:\n"
        "        pass\n"
    )

    assert cross_file._module_scope_rebound_names(text, path=_CALLER) == frozenset(
        {"record", "tail", "leftover", "whole"}
    )


@pytest.mark.unit
def test_a_global_declaration_counts_as_a_module_scope_rebinding() -> None:
    """``global f`` exists to assign ``f``, so the module binding is not proof."""
    text = (
        "from pkg import record\n"
        "\n"
        "\n"
        "def install(recorder):\n"
        "    global record\n"
        "    record = recorder\n"
    )

    assert cross_file._module_scope_rebound_names(text, path=_CALLER) == frozenset(
        {"install", "record"}
    )


@pytest.mark.unit
def test_unreadable_call_sites_report_no_module_scope_bindings() -> None:
    """A non-Python path and unparseable text both leave the bindings untouched."""
    assert cross_file._module_scope_rebound_names("record = 1\n", path="src/web/app.ts") == (
        frozenset()
    )
    assert cross_file._module_scope_rebound_names("record = (\n", path=_CALLER) == frozenset()
