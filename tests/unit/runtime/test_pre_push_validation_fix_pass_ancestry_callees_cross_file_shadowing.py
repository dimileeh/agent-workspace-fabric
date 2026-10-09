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
def test_loop_context_and_import_targets_of_the_anchored_scope_are_read() -> None:
    """Loop, ``with``, ``except`` and function-local import targets all bind the name."""
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
        {"paths", "looped", "opened", "late", "imported", "caught"}
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
