"""Effective-definition shadowing for cross-file callee evidence (issue #1019).

Unit tests for the other half of the shadowing rule: which of a candidate
module's same-named definitions the call site actually reaches. A module that
binds the imported name twice keeps only the last binding, and a later
assignment, import or star import rebinds it away entirely, so a correction to
the *dead* definition is no evidence about the call
(PRRT_kwDOSJAM6s6q9WnX). Split off
``test_pre_push_validation_fix_pass_ancestry_callees_cross_file_shadowing.py``
so both stay under the first-party file line limit.
"""

from __future__ import annotations

import pytest

from awf.runtime.pr_monitor_runner import (
    pre_push_validation_fix_pass_ancestry_cross_file as cross_file,
)
from tests.unit.runtime._pre_push_ancestry_cross_file_helpers import (
    _CALLEE_MODULE,
    _CALLER,
    _LEFT,
    _cross_package_probe,
    _Probe,
    _probe,
)

# The candidate module binds the imported name twice at module level: the first
# definition is dead, because the import reaches the last one executed.
_DUPLICATE_DEFINITION_CALLEE_TEXT = (
    "def record_ready_queue_depth(payload):\n"
    "    return None\n"
    "\n"
    "\n"
    "def record_ready_queue_depth(payload):\n"
    "    return len(payload.entries)\n"
)

# A body-only change inside the dead first definition's span (old line 2).
_DEAD_DEFINITION_DIFF = (
    f"--- a/{_CALLEE_MODULE}\n"
    f"+++ b/{_CALLEE_MODULE}\n"
    "@@ -2 +2 @@\n"
    "-    return None\n"
    "+    return 0\n"
)

# The same change inside the effective definition's span (old line 6).
_EFFECTIVE_DEFINITION_DIFF = (
    f"--- a/{_CALLEE_MODULE}\n"
    f"+++ b/{_CALLEE_MODULE}\n"
    "@@ -6 +6 @@\n"
    "-    return len(payload.entries)\n"
    "+    return payload.ready_depth()\n"
)


@pytest.mark.unit
def test_only_the_effective_module_scope_definition_is_importable() -> None:
    """Duplicate module-level definitions resolve to the last one executed.

    Python binds the imported name to the definition the module body runs last,
    so an earlier same-named definition is dead code no caller can reach and a
    correction confined to it is not a change to the callee
    (PRRT_kwDOSJAM6s6q9Wnf). Mirrors the same-file reader's rule in
    ``_resolve_callee_definition_span``. Members of distinct module-level
    classes are distinct attributes, not a shadowing pair, so both survive for
    an attribute-qualified callee.
    """
    assert cross_file._importable_definition_spans_for_names(
        _DUPLICATE_DEFINITION_CALLEE_TEXT,
        frozenset(),
        path=_CALLEE_MODULE,
        bare_names=frozenset({"record_ready_queue_depth"}),
    ) == [(5, 6)]
    assert cross_file._importable_definition_spans_for_names(
        _DUPLICATE_DEFINITION_CALLEE_TEXT,
        frozenset({"record_ready_queue_depth"}),
        path=_CALLEE_MODULE,
    ) == [(5, 6)]

    two_classes = (
        "class Primary:\n"
        "    def record(self, payload):\n"
        "        return payload\n"
        "\n"
        "\n"
        "class Secondary:\n"
        "    def record(self, payload):\n"
        "        return None\n"
    )
    assert cross_file._importable_definition_spans_for_names(
        two_classes, frozenset({"record"}), path=_CALLEE_MODULE
    ) == [(2, 5), (7, 8)]


@pytest.mark.unit
async def test_a_change_to_a_dead_duplicate_definition_is_not_evidence() -> None:
    """Editing the shadowed definition leaves the called one untouched."""
    probe = _cross_package_probe(
        callee_text=_DUPLICATE_DEFINITION_CALLEE_TEXT, diff=_DEAD_DEFINITION_DIFF
    )

    assert not await _probe(probe)


@pytest.mark.unit
async def test_a_change_to_the_effective_duplicate_definition_is_evidence() -> None:
    """The paired accept: the surviving last definition is the real callee."""
    probe = _cross_package_probe(
        callee_text=_DUPLICATE_DEFINITION_CALLEE_TEXT, diff=_EFFECTIVE_DEFINITION_DIFF
    )

    assert await _probe(probe)


# The callee module defines the imported name twice, in mutually exclusive
# branches: which definition the import reaches depends on the branch taken,
# not on textual order.
_CONDITIONAL_DUPLICATE_CALLEE_TEXT = (
    "import sys\n"
    "\n"
    "\n"
    'if sys.platform == "win32":\n'
    "\n"
    "    def record_ready_queue_depth(payload):\n"
    "        return payload.windows_depth()\n"
    "\n"
    "else:\n"
    "\n"
    "    def record_ready_queue_depth(payload):\n"
    "        return len(payload.entries)\n"
)

# A body-only change inside the textually last branch's definition (old line 12).
_LAST_BRANCH_DIFF = (
    f"--- a/{_CALLEE_MODULE}\n"
    f"+++ b/{_CALLEE_MODULE}\n"
    "@@ -12 +12 @@\n"
    "-        return len(payload.entries)\n"
    "+        return payload.ready_depth()\n"
)


@pytest.mark.unit
def test_conditionally_duplicated_definitions_fail_closed() -> None:
    """Branch-guarded duplicates leave no provable effective definition.

    ``if sys.platform == "win32": def validate(...)`` / ``else: def
    validate(...)`` binds the name from the branch the module runs, so the
    textually last head is not provably the one an importing call site reaches.
    Crediting a correction confined to it would mark the item fixed while the
    callable actually imported stays untouched, so every module-scope span of
    such a name is withheld (PRRT_kwDOSJAM6s6q-6LP).
    """
    for names, bare_names in (
        (frozenset(), frozenset({"record_ready_queue_depth"})),
        (frozenset({"record_ready_queue_depth"}), frozenset()),
    ):
        assert (
            cross_file._importable_definition_spans_for_names(
                _CONDITIONAL_DUPLICATE_CALLEE_TEXT,
                names,
                path=_CALLEE_MODULE,
                bare_names=bare_names,
            )
            == []
        )


@pytest.mark.unit
def test_a_single_conditional_definition_stays_importable() -> None:
    """One branch-guarded head is the only binding the call site can reach."""
    single = (
        "import sys\n"
        "\n"
        "\n"
        "if sys.platform:\n"
        "\n"
        "    def record_ready_queue_depth(payload):\n"
        "        return len(payload.entries)\n"
    )

    assert cross_file._importable_definition_spans_for_names(
        single,
        frozenset(),
        path=_CALLEE_MODULE,
        bare_names=frozenset({"record_ready_queue_depth"}),
    ) == [(6, 7)]


@pytest.mark.unit
def test_a_module_indent_last_head_still_shadows_a_conditional_one() -> None:
    """An unconditional head after a guarded one always rebinds the name."""
    rebound = (
        "import sys\n"
        "\n"
        "\n"
        "if sys.platform:\n"
        "\n"
        "    def record_ready_queue_depth(payload):\n"
        "        return payload.guarded\n"
        "\n"
        "\n"
        "def record_ready_queue_depth(payload):\n"
        "    return len(payload.entries)\n"
    )

    assert cross_file._importable_definition_spans_for_names(
        rebound,
        frozenset(),
        path=_CALLEE_MODULE,
        bare_names=frozenset({"record_ready_queue_depth"}),
    ) == [(10, 11)]


@pytest.mark.unit
async def test_a_change_to_one_branch_of_a_duplicate_definition_is_not_evidence() -> None:
    """End to end: the guarded pair withholds evidence instead of resolving."""
    probe = _cross_package_probe(
        callee_text=_CONDITIONAL_DUPLICATE_CALLEE_TEXT, diff=_LAST_BRANCH_DIFF
    )

    assert not await _probe(probe)


# One module-level class defining the same method twice: the class body binds
# the attribute to the later ``def``, so the earlier one is dead code no
# receiver reaches (PRRT_kwDOSJAM6s6q_M0o).
_DUPLICATE_METHOD_CALLEE_TEXT = (
    "class Collector:\n"
    "    def record_ready_queue_depth(self, payload):\n"
    "        return None\n"
    "\n"
    "    def record_ready_queue_depth(self, payload):\n"
    "        return len(payload.entries)\n"
)

# The same method name declared twice inside the class, in mutually exclusive
# branches: which one the attribute holds depends on the branch taken.
_CONDITIONAL_METHOD_CALLEE_TEXT = (
    "import sys\n"
    "\n"
    "\n"
    "class Collector:\n"
    "\n"
    '    if sys.platform == "win32":\n'
    "\n"
    "        def record_ready_queue_depth(self, payload):\n"
    "            return payload.windows_depth()\n"
    "\n"
    "    else:\n"
    "\n"
    "        def record_ready_queue_depth(self, payload):\n"
    "            return len(payload.entries)\n"
)

_DUPLICATE_METHOD_CALLER_TEXT = (
    "from pkg_b.observability.execution_platform_metrics import Collector\n"
    "\n"
    "\n"
    "def refresh(payload):\n"
    "    Collector.record_ready_queue_depth(payload)\n"
)

# A body-only change inside the dead first method's span (old line 3).
_DEAD_METHOD_DIFF = (
    f"--- a/{_CALLEE_MODULE}\n"
    f"+++ b/{_CALLEE_MODULE}\n"
    "@@ -3 +3 @@\n"
    "-        return None\n"
    "+        return 0\n"
)

# The same change inside the effective method's span (old line 6).
_EFFECTIVE_METHOD_DIFF = (
    f"--- a/{_CALLEE_MODULE}\n"
    f"+++ b/{_CALLEE_MODULE}\n"
    "@@ -6 +6 @@\n"
    "-        return len(payload.entries)\n"
    "+        return payload.ready_depth()\n"
)


def _duplicate_method_probe(*, diff: str) -> _Probe:
    return _Probe(
        texts={
            (_LEFT, _CALLER): _DUPLICATE_METHOD_CALLER_TEXT,
            (_LEFT, _CALLEE_MODULE): _DUPLICATE_METHOD_CALLEE_TEXT,
        },
        changed_paths=(_CALLEE_MODULE,),
        diffs={_CALLEE_MODULE: diff},
    )


@pytest.mark.unit
def test_only_the_effective_class_member_definition_is_importable() -> None:
    """A class that defines one method twice binds the later ``def``.

    The class body executes its heads in textual order just as a module body
    does, so an earlier same-named method is dead code the attribute never
    holds and a correction confined to it changes nothing the receiver calls
    (PRRT_kwDOSJAM6s6q_M0o). Holds whether or not the receiver's import pins
    the enclosing class.
    """
    assert cross_file._importable_definition_spans_for_names(
        _DUPLICATE_METHOD_CALLEE_TEXT,
        frozenset({"record_ready_queue_depth"}),
        path=_CALLEE_MODULE,
    ) == [(5, 6)]
    assert cross_file._importable_definition_spans_for_names(
        _DUPLICATE_METHOD_CALLEE_TEXT,
        frozenset({"record_ready_queue_depth"}),
        path=_CALLEE_MODULE,
        enclosed_by={"record_ready_queue_depth": frozenset({"Collector"})},
    ) == [(5, 6)]


@pytest.mark.unit
def test_conditionally_duplicated_class_members_fail_closed() -> None:
    """Branch-guarded members leave no provable effective method.

    A head indented deeper than its class body runs only when its enclosing
    block does, so the textually last of two guarded ``def``s is not provably
    the attribute the receiver reaches; every span of that name in that class
    is withheld (PRRT_kwDOSJAM6s6q_M0o).
    """
    assert (
        cross_file._importable_definition_spans_for_names(
            _CONDITIONAL_METHOD_CALLEE_TEXT,
            frozenset({"record_ready_queue_depth"}),
            path=_CALLEE_MODULE,
            enclosed_by={"record_ready_queue_depth": frozenset({"Collector"})},
        )
        == []
    )


@pytest.mark.unit
async def test_a_change_to_a_dead_duplicate_class_member_is_not_evidence() -> None:
    """End to end: editing the shadowed method leaves the called one untouched."""
    assert not await _probe(_duplicate_method_probe(diff=_DEAD_METHOD_DIFF), item_line=5)


@pytest.mark.unit
async def test_a_change_to_the_effective_duplicate_class_member_is_evidence() -> None:
    """The paired accept: the surviving last method is the real callee."""
    assert await _probe(_duplicate_method_probe(diff=_EFFECTIVE_METHOD_DIFF), item_line=5)


@pytest.mark.unit
def test_bindings_owned_only_by_a_nested_scope_do_not_shadow() -> None:
    """A child scope's own parameters and locals are not the parent's locals.

    A nested helper assigning ``validate``, a lambda taking it as a parameter
    and a comprehension using it as a target all bind the name in *their* own
    scope, so the anchored line's ``validate`` still holds the module's import
    and a correction to that imported definition does change the callee it
    reaches (PRRT_kwDOSJAM6s6q_M0s). What those children bind in the parent —
    the nested definition's name, the assignment the comprehension is stored
    to — is still reported.
    """
    text = (
        "def refresh(pool):\n"
        "    def helper(validate):\n"
        "        shadowed = validate\n"
        "        return shadowed\n"
        "\n"
        "    picked = [validate for validate in pool.items]\n"
        "    mapper = lambda validate: validate\n"
        "    return validate(helper, picked, mapper)\n"
    )

    assert cross_file._locally_rebound_names_at_line(text, 8, path=_CALLER) == frozenset(
        {"pool", "helper", "picked", "mapper"}
    )


@pytest.mark.unit
def test_a_comprehension_target_shadows_inside_the_comprehension() -> None:
    """The paired fail-closed: a call *inside* the comprehension reaches its target."""
    text = (
        "def refresh(pool):\n"
        "    return [\n"
        "        validate(item)\n"
        "        for validate, item in pool.pairs\n"
        "    ]\n"
    )

    assert cross_file._locally_rebound_names_at_line(text, 3, path=_CALLER) == frozenset(
        {"pool", "validate", "item"}
    )


@pytest.mark.unit
def test_a_walrus_inside_a_comprehension_still_binds_the_holding_scope() -> None:
    """``:=`` assigns in the scope holding the comprehension, so it shadows there.

    A walrus a child *definition* evaluates reaches the enclosing scope too
    (through a decorator or a parameter default), so every ``:=`` target in a
    child keeps failing closed rather than paying to tell its position apart.
    """
    text = (
        "def refresh(pool):\n"
        "    totals = [(validate := item.check)(item) for item in pool.items]\n"
        "    hooks = [lambda item=item: (inner := item) for item in pool.items]\n"
        "    return validate, totals, hooks\n"
    )

    assert cross_file._locally_rebound_names_at_line(text, 4, path=_CALLER) == frozenset(
        {"pool", "totals", "hooks", "validate", "inner"}
    )


# The candidate module defines the imported name and then rebinds it to
# something that carries no definition head of its own, so importing the name
# reaches the replacement and the ``def`` above it is dead code.
_REASSIGNED_DEFINITION_CALLEE_TEXT = (
    "def record_ready_queue_depth(payload):\n"
    "    return None\n"
    "\n"
    "\n"
    "record_ready_queue_depth = _build_recorder()\n"
)

# A body-only change inside the shadowed definition's span (old line 2).
_REASSIGNED_DEAD_DEFINITION_DIFF = (
    f"--- a/{_CALLEE_MODULE}\n"
    f"+++ b/{_CALLEE_MODULE}\n"
    "@@ -2 +2 @@\n"
    "-    return None\n"
    "+    return 0\n"
)


@pytest.mark.unit
def test_a_definition_a_plain_assignment_rebinds_is_not_importable() -> None:
    """``def f(...)`` followed by ``f = replacement`` leaves the ``def`` dead.

    The module body runs the assignment last, so the imported name holds the
    replacement and a correction confined to the earlier ``def`` changes
    nothing the call site reaches. Only recognized definition heads are
    collected, so that assignment is invisible to the last-head fold and the
    name has to fail closed here instead (PRRT_kwDOSJAM6s6q_ywa). An
    assignment whose right-hand side *is* a definition head keeps folding
    normally, and an assignment of some other name shadows nothing.
    """
    for names, bare_names in (
        (frozenset({"record_ready_queue_depth"}), frozenset()),
        (frozenset(), frozenset({"record_ready_queue_depth"})),
    ):
        assert (
            cross_file._importable_definition_spans_for_names(
                _REASSIGNED_DEFINITION_CALLEE_TEXT,
                names,
                path=_CALLEE_MODULE,
                bare_names=bare_names,
            )
            == []
        )

    rebound_to_lambda = _REASSIGNED_DEFINITION_CALLEE_TEXT.replace(
        "_build_recorder()", "lambda payload: payload.ready_depth()"
    )
    assert cross_file._importable_definition_spans_for_names(
        rebound_to_lambda,
        frozenset(),
        path=_CALLEE_MODULE,
        bare_names=frozenset({"record_ready_queue_depth"}),
    ) == [(5, 5)]

    other_name = _REASSIGNED_DEFINITION_CALLEE_TEXT.replace(
        "record_ready_queue_depth = _build_recorder()", "RECORDER = _build_recorder()"
    )
    assert cross_file._importable_definition_spans_for_names(
        other_name,
        frozenset(),
        path=_CALLEE_MODULE,
        bare_names=frozenset({"record_ready_queue_depth"}),
    ) == [(1, 4)]


@pytest.mark.unit
def test_a_class_member_a_plain_assignment_rebinds_is_not_importable() -> None:
    """A class body rebinding the attribute leaves the earlier ``def`` dead too.

    ``record = staticmethod(record)`` is the attribute the receiver reaches, so
    the method above it is not the callee a correction has to touch
    (PRRT_kwDOSJAM6s6q_ywa).
    """
    text = (
        "class Collector:\n"
        "    def record_ready_queue_depth(self, payload):\n"
        "        return len(payload.entries)\n"
        "\n"
        "    record_ready_queue_depth = staticmethod(_recorder)\n"
    )

    assert (
        cross_file._importable_definition_spans_for_names(
            text,
            frozenset({"record_ready_queue_depth"}),
            path=_CALLEE_MODULE,
            enclosed_by={"record_ready_queue_depth": frozenset({"Collector"})},
        )
        == []
    )


@pytest.mark.unit
def test_bindings_outside_the_definition_scope_do_not_shadow_it() -> None:
    """Only the binding scope's own statements rebind its names.

    A keyword argument and a continuation-line parameter default read as
    ``name=value`` but are not statements, and a function-local assignment
    binds in that function, so none of them makes the module-level definition
    unreachable (PRRT_kwDOSJAM6s6q_ywa).
    """
    text = (
        "def record_ready_queue_depth(payload):\n"
        "    return len(payload.entries)\n"
        "\n"
        "\n"
        "GAUGE = _build_gauge(\n"
        "    record_ready_queue_depth=None,\n"
        ")\n"
        "\n"
        "\n"
        "def refresh(\n"
        "    record_ready_queue_depth=None,\n"
        "):\n"
        "    record_ready_queue_depth = GAUGE.recorder\n"
        "    return record_ready_queue_depth\n"
    )

    assert cross_file._importable_definition_spans_for_names(
        text,
        frozenset(),
        path=_CALLEE_MODULE,
        bare_names=frozenset({"record_ready_queue_depth"}),
    ) == [(1, 4)]


@pytest.mark.unit
async def test_a_change_to_a_definition_a_later_assignment_rebinds_is_not_evidence() -> None:
    """End to end: the shadowed ``def`` withholds evidence instead of resolving."""
    probe = _cross_package_probe(
        callee_text=_REASSIGNED_DEFINITION_CALLEE_TEXT,
        diff=_REASSIGNED_DEAD_DEFINITION_DIFF,
    )

    assert not await _probe(probe)


@pytest.mark.unit
def test_a_class_body_binding_shadows_for_a_call_in_the_class_body() -> None:
    """A line executing directly in a class body resolves through the class namespace.

    ``class C: validate = local_validate; result = validate()`` calls the class
    attribute, not the module's import, so the class body's own bindings shadow
    at that anchor (PRRT_kwDOSJAM6s6q_ywe) — while the method body below keeps
    the import, which is the paired tolerance
    ``test_a_class_body_binding_is_not_a_local_shadow`` covers.
    """
    text = (
        "class Collector:\n"
        "    validate = local_validate\n"
        "    result = validate()\n"
        "\n"
        "    def refresh(self):\n"
        "        return validate()\n"
    )

    assert cross_file._locally_rebound_names_at_line(text, 3, path=_CALLER) == frozenset(
        {"validate", "result", "refresh"}
    )
    assert cross_file._locally_rebound_names_at_line(text, 6, path=_CALLER) == frozenset({"self"})


@pytest.mark.unit
def test_the_innermost_class_body_is_the_one_read_at_its_own_anchor() -> None:
    """Only the innermost class body binds, and an enclosing function still does.

    A class body does not see the class body around it, while it *does* see the
    locals of a function that holds it, so a nested class's anchor reads its own
    attributes plus the enclosing function's locals and nothing from the outer
    class (PRRT_kwDOSJAM6s6q_ywe).
    """
    text = (
        "class Outer:\n"
        "    outer_attr = 1\n"
        "\n"
        "    def build(self, pool):\n"
        "        class Inner:\n"
        "            validate = pool.check\n"
        "            result = validate()\n"
        "\n"
        "        return Inner\n"
    )

    assert cross_file._locally_rebound_names_at_line(text, 7, path=_CALLER) == frozenset(
        {"self", "pool", "Inner", "validate", "result"}
    )


# The candidate module defines the imported name and then imports the same name
# from somewhere else, so a caller importing it reaches the fallback object and
# the ``def`` above is dead code.
_IMPORT_SHADOWED_DEFINITION_CALLEE_TEXT = (
    "def record_ready_queue_depth(payload):\n"
    "    return None\n"
    "\n"
    "\n"
    "from pkg_b.fallback import record_ready_queue_depth\n"
)


@pytest.mark.unit
def test_a_definition_a_later_import_rebinds_is_not_importable() -> None:
    """``def f(...)`` plus ``from fallback import f`` leaves the ``def`` dead.

    The import binds the name to the fallback object, so a correction confined
    to the earlier head changes nothing the importing call site reaches. Import
    statements carry no definition head, so they are invisible to the last-head
    fold and the name has to fail closed here (PRRT_kwDOSJAM6s6rAAWY). An
    aliased import, a plain ``import`` of the name and a parenthesized import
    list all bind it the same way; an import of some other name, and an import
    inside a function body, shadow nothing.
    """
    for names, bare_names in (
        (frozenset({"record_ready_queue_depth"}), frozenset()),
        (frozenset(), frozenset({"record_ready_queue_depth"})),
    ):
        assert (
            cross_file._importable_definition_spans_for_names(
                _IMPORT_SHADOWED_DEFINITION_CALLEE_TEXT,
                names,
                path=_CALLEE_MODULE,
                bare_names=bare_names,
            )
            == []
        )

    for rebinding_import in (
        "from pkg_b.fallback import build_recorder as record_ready_queue_depth",
        "import record_ready_queue_depth",
        "from pkg_b.fallback import (\n    record_ready_queue_depth,\n)",
    ):
        text = _IMPORT_SHADOWED_DEFINITION_CALLEE_TEXT.replace(
            "from pkg_b.fallback import record_ready_queue_depth", rebinding_import
        )
        assert (
            cross_file._importable_definition_spans_for_names(
                text,
                frozenset(),
                path=_CALLEE_MODULE,
                bare_names=frozenset({"record_ready_queue_depth"}),
            )
            == []
        )

    for inert_import in (
        "from pkg_b.fallback import build_recorder",
        "def refresh():\n    from pkg_b.fallback import record_ready_queue_depth\n",
    ):
        text = _IMPORT_SHADOWED_DEFINITION_CALLEE_TEXT.replace(
            "from pkg_b.fallback import record_ready_queue_depth", inert_import
        )
        assert cross_file._importable_definition_spans_for_names(
            text,
            frozenset(),
            path=_CALLEE_MODULE,
            bare_names=frozenset({"record_ready_queue_depth"}),
        ) == [(1, 4)]


@pytest.mark.unit
def test_a_star_import_withholds_every_definition_it_could_rebind() -> None:
    """``from pkg import *`` binds names this reader cannot enumerate.

    It may well bind ``record_ready_queue_depth``, and nothing in the file says
    otherwise, so the definition fails closed rather than being offered as the
    importable callee (PRRT_kwDOSJAM6s6rAAWY).
    """
    text = _IMPORT_SHADOWED_DEFINITION_CALLEE_TEXT.replace(
        "import record_ready_queue_depth", "import *"
    )

    assert (
        cross_file._importable_definition_spans_for_names(
            text,
            frozenset(),
            path=_CALLEE_MODULE,
            bare_names=frozenset({"record_ready_queue_depth"}),
        )
        == []
    )


@pytest.mark.unit
def test_a_class_member_an_import_rebinds_is_not_importable() -> None:
    """A class body importing the attribute leaves the earlier method dead too.

    The class attribute the receiver reaches is the imported object, not the
    method above it (PRRT_kwDOSJAM6s6rAAWY).
    """
    text = (
        "class Collector:\n"
        "    def record_ready_queue_depth(self, payload):\n"
        "        return len(payload.entries)\n"
        "\n"
        "    from pkg_b.fallback import record_ready_queue_depth\n"
    )

    assert (
        cross_file._importable_definition_spans_for_names(
            text,
            frozenset({"record_ready_queue_depth"}),
            path=_CALLEE_MODULE,
            enclosed_by={"record_ready_queue_depth": frozenset({"Collector"})},
        )
        == []
    )


@pytest.mark.unit
def test_a_non_python_candidate_keeps_its_definition_spans() -> None:
    """Text this reader cannot parse as Python leaves the lexical spans alone.

    A JS/TS module cannot both declare and import the same name — that is a
    redeclaration error — so the parse failure must not fail its definition
    spans closed (PRRT_kwDOSJAM6s6rAAWY).
    """
    text = (
        'import { recordReadyQueueDepth } from "./fallback";\n'
        "\n"
        "export function recordReadyQueueDepth(payload) {\n"
        "  return null;\n"
        "}\n"
    )

    assert cross_file._importable_definition_spans_for_names(
        text,
        frozenset(),
        path="src/pkg_b/observability/metrics.ts",
        bare_names=frozenset({"recordReadyQueueDepth"}),
    ) == [(3, 5)]


@pytest.mark.unit
async def test_a_change_to_a_definition_a_later_import_rebinds_is_not_evidence() -> None:
    """End to end: the import-shadowed ``def`` withholds evidence instead of resolving."""
    probe = _cross_package_probe(
        callee_text=_IMPORT_SHADOWED_DEFINITION_CALLEE_TEXT,
        diff=_REASSIGNED_DEAD_DEFINITION_DIFF,
    )

    assert not await _probe(probe)


@pytest.mark.unit
def test_a_rebinding_after_a_semicolon_on_the_same_line_is_not_importable() -> None:
    """A rebinding that follows another statement on its line still fails closed.

    A logical line can spell several simple statements, so
    ``_initialize(); record = replacement`` rebinds the name just as a line of
    its own would. Matched only from the start of the physical line, the
    assignment is invisible and the dead ``def`` above it is offered as the
    callee a correction must touch (PRRT_kwDOSJAM6s6rBTTP). A head line carries
    the same shape: the head itself is still not read as its own rebinding,
    while a statement after it is. A trailing statement binding some *other*
    name shadows nothing, as one on a line of its own does not.
    """
    trailing_rebind = (
        "def record_ready_queue_depth(payload):\n"
        "    return None\n"
        "\n"
        "\n"
        "_initialize(); record_ready_queue_depth = _build_recorder()\n"
    )
    assert (
        cross_file._importable_definition_spans_for_names(
            trailing_rebind,
            frozenset({"record_ready_queue_depth"}),
            path=_CALLEE_MODULE,
        )
        == []
    )

    after_a_head = (
        "record_ready_queue_depth = lambda payload: payload.ready_depth()"
        "; record_ready_queue_depth = _build_recorder()\n"
    )
    assert (
        cross_file._importable_definition_spans_for_names(
            after_a_head,
            frozenset({"record_ready_queue_depth"}),
            path=_CALLEE_MODULE,
        )
        == []
    )

    other_name = trailing_rebind.replace(
        "record_ready_queue_depth = _build_recorder()", "RECORDER = _build_recorder()"
    )
    assert cross_file._importable_definition_spans_for_names(
        other_name,
        frozenset({"record_ready_queue_depth"}),
        path=_CALLEE_MODULE,
    ) == [(1, 4)]


@pytest.mark.unit
def test_a_rebinding_inside_a_one_line_compound_suite_is_not_importable() -> None:
    """A suite written on its header's own line rebinds the name just as well.

    ``if enabled: record = replacement`` spells its assignment past the ``if``
    header, where neither the header nor the statement sits at the line's left
    edge, so a reader anchored there matches nothing and leaves the dead ``def``
    above it standing as the callee a correction must touch
    (PRRT_kwDOSJAM6s6rBbdn). The suite's own scope still decides: a
    function-local one binds a local, an annotated or unpacking target binds
    its names all the same, and a statement binding some *other* name — or an
    attribute or subscript of it, which is no name of the scope — shadows
    nothing.
    """
    guarded_rebind = (
        "def record_ready_queue_depth(payload):\n"
        "    return None\n"
        "\n"
        "\n"
        "if _enabled(): record_ready_queue_depth = _build_recorder()\n"
    )
    assert (
        cross_file._importable_definition_spans_for_names(
            guarded_rebind,
            frozenset({"record_ready_queue_depth"}),
            path=_CALLEE_MODULE,
        )
        == []
    )

    in_a_class_body = (
        "class Collector:\n"
        "    def record_ready_queue_depth(self, payload):\n"
        "        return len(payload.entries)\n"
        "\n"
        "    if _enabled(): record_ready_queue_depth = staticmethod(_recorder)\n"
    )
    assert (
        cross_file._importable_definition_spans_for_names(
            in_a_class_body,
            frozenset({"record_ready_queue_depth"}),
            path=_CALLEE_MODULE,
            enclosed_by={"record_ready_queue_depth": frozenset({"Collector"})},
        )
        == []
    )

    function_local = (
        "def record_ready_queue_depth(payload):\n"
        "    return len(payload.entries)\n"
        "\n"
        "\n"
        "def refresh(gauge):\n"
        "    if gauge: record_ready_queue_depth = gauge.recorder\n"
        "    return record_ready_queue_depth\n"
    )
    assert cross_file._importable_definition_spans_for_names(
        function_local,
        frozenset({"record_ready_queue_depth"}),
        path=_CALLEE_MODULE,
    ) == [(1, 4)]

    for binding in (
        "record_ready_queue_depth: Recorder = _build_recorder()",
        "record_ready_queue_depth, _tail = _build_recorders()",
        "[record_ready_queue_depth, *_rest] = _build_recorders()",
    ):
        assert (
            cross_file._importable_definition_spans_for_names(
                guarded_rebind.replace("record_ready_queue_depth = _build_recorder()", binding),
                frozenset({"record_ready_queue_depth"}),
                path=_CALLEE_MODULE,
            )
            == []
        ), binding

    for untouched in (
        "RECORDER = _build_recorder()",
        "_gauge.record_ready_queue_depth = _build_recorder()",
        "_gauge[record_ready_queue_depth] = _build_recorder()",
    ):
        assert cross_file._importable_definition_spans_for_names(
            guarded_rebind.replace("record_ready_queue_depth = _build_recorder()", untouched),
            frozenset({"record_ready_queue_depth"}),
            path=_CALLEE_MODULE,
        ) == [(1, 4)], untouched


@pytest.mark.unit
def test_a_walrus_rebinding_in_a_compound_header_is_not_importable() -> None:
    """``:=`` binds the name inside the header, where no statement starts.

    A compound suite at least spells its assignment as a statement; a walrus
    binding lives in the header's own expression, so a reader that looks for
    statement starts has nothing to match either way. The importer still
    reaches the replacement, so the dead ``def`` above it is not the callee a
    correction must touch (PRRT_kwDOSJAM6s6rBbdn). The suite's own scope still
    decides: a function-local walrus binds a local, and one binding some other
    name shadows nothing.
    """
    guarded_rebind = (
        "def record_ready_queue_depth(payload):\n"
        "    return None\n"
        "\n"
        "\n"
        "if (record_ready_queue_depth := _build_recorder()) is not None:\n"
        "    _register(record_ready_queue_depth)\n"
    )
    assert (
        cross_file._importable_definition_spans_for_names(
            guarded_rebind,
            frozenset({"record_ready_queue_depth"}),
            path=_CALLEE_MODULE,
        )
        == []
    )

    function_local = (
        "def record_ready_queue_depth(payload):\n"
        "    return len(payload.entries)\n"
        "\n"
        "\n"
        "def refresh(gauge):\n"
        "    if (record_ready_queue_depth := gauge.recorder) is not None:\n"
        "        return record_ready_queue_depth\n"
        "    return None\n"
    )
    assert cross_file._importable_definition_spans_for_names(
        function_local,
        frozenset({"record_ready_queue_depth"}),
        path=_CALLEE_MODULE,
    ) == [(1, 4)]

    other_name = guarded_rebind.replace(
        "record_ready_queue_depth := _build_recorder()", "recorder := _build_recorder()"
    )
    assert cross_file._importable_definition_spans_for_names(
        other_name,
        frozenset({"record_ready_queue_depth"}),
        path=_CALLEE_MODULE,
    ) == [(1, 4)]


@pytest.mark.unit
def test_a_loop_or_with_target_rebinding_is_not_importable() -> None:
    """A ``for`` or ``with`` target rebinds the name as plainly as ``=`` does.

    ``for record in handlers:`` leaves the module-level ``def record`` above it
    dead once the loop has run, and ``with _recorder() as record:`` rebinds the
    name for its whole suite, so a reader watching only assignments still
    offers that dead head as the callee a correction must touch
    (PRRT_kwDOSJAM6s6rBjC4). The binding's own scope still decides: a
    function-local loop binds a local, an unpacking target binds its names all
    the same, and a target that is no name of the scope — a comprehension's
    own target, an attribute, a subscript, some other name, or a ``with`` item
    with no ``as`` at all — shadows nothing.
    """
    loop_rebind = (
        "def record_ready_queue_depth(payload):\n"
        "    return None\n"
        "\n"
        "\n"
        "for record_ready_queue_depth in _recorders():\n"
        "    _register(record_ready_queue_depth)\n"
    )
    assert (
        cross_file._importable_definition_spans_for_names(
            loop_rebind,
            frozenset({"record_ready_queue_depth"}),
            path=_CALLEE_MODULE,
        )
        == []
    )

    with_rebind = (
        "def record_ready_queue_depth(payload):\n"
        "    return None\n"
        "\n"
        "\n"
        "with _recorder() as record_ready_queue_depth:\n"
        "    _register(record_ready_queue_depth)\n"
    )
    assert (
        cross_file._importable_definition_spans_for_names(
            with_rebind,
            frozenset({"record_ready_queue_depth"}),
            path=_CALLEE_MODULE,
        )
        == []
    )

    in_a_class_body = (
        "class Collector:\n"
        "    def record_ready_queue_depth(self, payload):\n"
        "        return len(payload.entries)\n"
        "\n"
        "    for record_ready_queue_depth in _recorders():\n"
        "        pass\n"
    )
    assert (
        cross_file._importable_definition_spans_for_names(
            in_a_class_body,
            frozenset({"record_ready_queue_depth"}),
            path=_CALLEE_MODULE,
            enclosed_by={"record_ready_queue_depth": frozenset({"Collector"})},
        )
        == []
    )

    function_local = (
        "def record_ready_queue_depth(payload):\n"
        "    return len(payload.entries)\n"
        "\n"
        "\n"
        "def refresh(gauges):\n"
        "    for record_ready_queue_depth in gauges:\n"
        "        _register(record_ready_queue_depth)\n"
    )
    assert cross_file._importable_definition_spans_for_names(
        function_local,
        frozenset({"record_ready_queue_depth"}),
        path=_CALLEE_MODULE,
    ) == [(1, 4)]

    for binding in (
        "for record_ready_queue_depth, _tail in _recorders():",
        "for [record_ready_queue_depth, *_rest] in _recorders():",
    ):
        assert (
            cross_file._importable_definition_spans_for_names(
                loop_rebind.replace("for record_ready_queue_depth in _recorders():", binding),
                frozenset({"record_ready_queue_depth"}),
                path=_CALLEE_MODULE,
            )
            == []
        ), binding

    for untouched in (
        "for recorder in _recorders():",
        "for _gauge.record_ready_queue_depth in _recorders():",
        "for _gauge[record_ready_queue_depth] in _recorders():",
    ):
        assert cross_file._importable_definition_spans_for_names(
            loop_rebind.replace("for record_ready_queue_depth in _recorders():", untouched).replace(
                "_register(record_ready_queue_depth)", "pass"
            ),
            frozenset({"record_ready_queue_depth"}),
            path=_CALLEE_MODULE,
        ) == [(1, 4)], untouched

    comprehension_target = (
        "def record_ready_queue_depth(payload):\n"
        "    return None\n"
        "\n"
        "\n"
        "_RECORDERS = [record_ready_queue_depth for record_ready_queue_depth in _recorders()]\n"
    )
    assert cross_file._importable_definition_spans_for_names(
        comprehension_target,
        frozenset({"record_ready_queue_depth"}),
        path=_CALLEE_MODULE,
    ) == [(1, 4)]

    no_as_clause = with_rebind.replace(
        "with _recorder() as record_ready_queue_depth:", "with _recorder():"
    ).replace("_register(record_ready_queue_depth)", "pass")
    assert cross_file._importable_definition_spans_for_names(
        no_as_clause,
        frozenset({"record_ready_queue_depth"}),
        path=_CALLEE_MODULE,
    ) == [(1, 4)]


@pytest.mark.unit
def test_a_binding_in_a_one_line_definition_suite_is_not_a_module_rebinding() -> None:
    """A suite on its header's own line binds in *that* header's scope.

    ``def refresh(gauge): record = gauge.recorder`` spells a local, but the
    statement carries the ``def``'s own indent on the ``def``'s own line, which
    the span reader resolves to the scope *enclosing* the header — so the local
    looked like a module-scope rebinding, the live module ``def`` was withheld
    and a correct callee-span fix parked as ``needs_human``
    (PRRT_kwDOSJAM6s6rBkXd). A one-line ``class`` body and a one-line
    function-local ``import`` bind no module name either, while a one-line
    suite that is no definition head at all — ``if enabled:`` — still rebinds
    the module name and still fails closed.
    """
    for one_liner in (
        "def refresh(gauge): record_ready_queue_depth = gauge.recorder",
        "async def refresh(gauge): record_ready_queue_depth = gauge.recorder",
        "def refresh(gauge): record_ready_queue_depth: Recorder = gauge.recorder",
        "def refresh(gauge): return (record_ready_queue_depth := gauge.recorder)",
        "def refresh(gauge): from pkg_b.fallback import record_ready_queue_depth",
        "class Collector: record_ready_queue_depth = staticmethod(_recorder)",
    ):
        assert cross_file._importable_definition_spans_for_names(
            "def record_ready_queue_depth(payload):\n"
            "    return len(payload.entries)\n"
            "\n"
            "\n"
            f"{one_liner}\n",
            frozenset({"record_ready_queue_depth"}),
            path=_CALLEE_MODULE,
        ) == [(1, 4)], one_liner

    in_a_class_body = (
        "class Collector:\n"
        "    def record_ready_queue_depth(self, payload):\n"
        "        return len(payload.entries)\n"
        "\n"
        "    def refresh(self, gauge): record_ready_queue_depth = gauge.recorder\n"
    )
    assert cross_file._importable_definition_spans_for_names(
        in_a_class_body,
        frozenset({"record_ready_queue_depth"}),
        path=_CALLEE_MODULE,
        enclosed_by={"record_ready_queue_depth": frozenset({"Collector"})},
    ) == [(2, 4)]

    for module_rebind in (
        "if _enabled(): record_ready_queue_depth = _build_recorder()",
        "if _enabled(): from pkg_b.fallback import record_ready_queue_depth",
    ):
        assert (
            cross_file._importable_definition_spans_for_names(
                "def record_ready_queue_depth(payload):\n"
                "    return len(payload.entries)\n"
                "\n"
                "\n"
                f"{module_rebind}\n",
                frozenset({"record_ready_queue_depth"}),
                path=_CALLEE_MODULE,
            )
            == []
        ), module_rebind


@pytest.mark.unit
def test_a_one_line_suite_on_a_wrapped_header_is_not_a_module_rebinding() -> None:
    """A one-line suite binds in its header's scope however the header is written.

    The header's own physical line is not the only place a one-line suite can
    sit: a wrapped signature puts it on the closing ``): `` line, which carries
    the *header's* indent just the same and still starts on no line the
    header's lexical span contains. Reading only the head's own line left that
    local looking like a module-scope rebinding, so the live module ``def`` was
    withheld and a correct callee-span fix parked as ``needs_human``
    (PRRT_kwDOSJAM6s6rBkXd). A wrapped header that is no definition at all —
    ``if _enabled(...)`` — still rebinds the module name and still fails
    closed.
    """
    for one_liner in (
        "): record_ready_queue_depth = gauge.recorder",
        "): from pkg_b.fallback import record_ready_queue_depth",
    ):
        assert cross_file._importable_definition_spans_for_names(
            "def record_ready_queue_depth(payload):\n"
            "    return len(payload.entries)\n"
            "\n"
            "\n"
            "def refresh(\n"
            "    gauge,\n"
            f"{one_liner}\n",
            frozenset({"record_ready_queue_depth"}),
            path=_CALLEE_MODULE,
        ) == [(1, 4)], one_liner

    assert (
        cross_file._importable_definition_spans_for_names(
            "def record_ready_queue_depth(payload):\n"
            "    return len(payload.entries)\n"
            "\n"
            "\n"
            "if _enabled(\n"
            "    flag,\n"
            "): record_ready_queue_depth = _build_recorder()\n",
            frozenset({"record_ready_queue_depth"}),
            path=_CALLEE_MODULE,
        )
        == []
    )


@pytest.mark.unit
def test_a_match_capture_rebinding_is_not_importable() -> None:
    """A ``match`` pattern binds its capture as plainly as ``=`` does.

    ``case record:`` replaces the module-level ``def record`` above it once the
    pattern matches, and a ``*rest`` or ``**rest`` capture does the same, so a
    reader watching only assignment statements still offers the dead head as
    the callee a correction must touch while importers receive the captured
    value. The capture's own scope still decides: one inside a function binds a
    local and shadows nothing at module scope.
    """
    for pattern in (
        "case record_ready_queue_depth:",
        "case [*record_ready_queue_depth]:",
        "case {**record_ready_queue_depth}:",
    ):
        module_capture = (
            "def record_ready_queue_depth(payload):\n"
            "    return None\n"
            "\n"
            "\n"
            "match _handler():\n"
            f"    {pattern}\n"
            "        pass\n"
        )
        assert (
            cross_file._importable_definition_spans_for_names(
                module_capture,
                frozenset({"record_ready_queue_depth"}),
                path=_CALLEE_MODULE,
            )
            == []
        )

    function_local = (
        "def record_ready_queue_depth(payload):\n"
        "    return len(payload.entries)\n"
        "\n"
        "\n"
        "def refresh(handler):\n"
        "    match handler:\n"
        "        case record_ready_queue_depth:\n"
        "            return record_ready_queue_depth\n"
    )
    assert cross_file._importable_definition_spans_for_names(
        function_local,
        frozenset({"record_ready_queue_depth"}),
        path=_CALLEE_MODULE,
    ) == [(1, 4)]


@pytest.mark.unit
def test_a_global_declared_rebinding_is_attributed_to_module_scope() -> None:
    """``global record`` makes a function-body assignment a module rebinding.

    ``global`` exists only so the assignment reaches the module binding, so a
    helper carrying one replaces the ``def`` above it for every importer once
    it runs — attributing that assignment to the helper's own syntactic scope
    leaves the dead head importable and lets a correction confined to it
    resolve the thread. Without the declaration the same statement binds a
    local and shadows nothing, and a declaration naming some *other* name
    leaves the binding local too.
    """
    global_rebind = (
        "def record_ready_queue_depth(payload):\n"
        "    return None\n"
        "\n"
        "\n"
        "def _install():\n"
        "    global record_ready_queue_depth\n"
        "    record_ready_queue_depth = _build_recorder()\n"
        "\n"
        "\n"
        "_install()\n"
    )
    assert (
        cross_file._importable_definition_spans_for_names(
            global_rebind,
            frozenset({"record_ready_queue_depth"}),
            path=_CALLEE_MODULE,
        )
        == []
    )

    local_only = global_rebind.replace("    global record_ready_queue_depth\n", "")
    assert cross_file._importable_definition_spans_for_names(
        local_only,
        frozenset({"record_ready_queue_depth"}),
        path=_CALLEE_MODULE,
    ) == [(1, 4)]

    other_name = global_rebind.replace(
        "    global record_ready_queue_depth\n", "    global _recorder\n"
    )
    assert cross_file._importable_definition_spans_for_names(
        other_name,
        frozenset({"record_ready_queue_depth"}),
        path=_CALLEE_MODULE,
    ) == [(1, 4)]


@pytest.mark.unit
def test_a_global_declared_import_is_attributed_to_module_scope() -> None:
    """``global record`` makes a function-body *import* a module rebinding too.

    The same reading the declaration forces on an assignment, on the one other
    statement form that binds a name without carrying a definition head of its
    own: ``global record`` beside ``from fallback import record`` replaces the
    module binding for every importer once the helper runs, so attributing the
    import to the helper's syntactic scope left the dead ``def`` importable and
    let a correction confined to it satisfy the cross-file evidence gate while
    callers reach the fallback object (PRRT_kwDOSJAM6s6rB2mf). Without the
    declaration the import binds a local and shadows nothing, and a declaration
    naming some *other* name leaves it local as well.
    """
    global_import = (
        "def record_ready_queue_depth(payload):\n"
        "    return None\n"
        "\n"
        "\n"
        "def _install():\n"
        "    global record_ready_queue_depth\n"
        "    from fallback import record_ready_queue_depth\n"
        "\n"
        "\n"
        "_install()\n"
    )
    assert (
        cross_file._importable_definition_spans_for_names(
            global_import,
            frozenset({"record_ready_queue_depth"}),
            path=_CALLEE_MODULE,
        )
        == []
    )

    local_only = global_import.replace("    global record_ready_queue_depth\n", "")
    assert cross_file._importable_definition_spans_for_names(
        local_only,
        frozenset({"record_ready_queue_depth"}),
        path=_CALLEE_MODULE,
    ) == [(1, 4)]

    other_name = global_import.replace(
        "    global record_ready_queue_depth\n", "    global _recorder\n"
    )
    assert cross_file._importable_definition_spans_for_names(
        other_name,
        frozenset({"record_ready_queue_depth"}),
        path=_CALLEE_MODULE,
    ) == [(1, 4)]


@pytest.mark.unit
def test_exception_handler_targets_rebind_the_name() -> None:
    """``except Exception as record`` leaves no module binding of that name.

    A handler target binds its name for the handler's body and then Python
    *deletes* it on the way out, so a module body whose handler runs replaces
    the ``def record`` above it and then unbinds the name entirely: importing
    ``record`` fails. Reading only assignment and loop targets still offered
    that dead head as the callee a correction has to touch, so an edit confined
    to it resolved the thread even though no importer can reach it at all
    (PRRT_kwDOSJAM6s6rB2mh).
    """
    handler_rebind = (
        "def record_ready_queue_depth(payload):\n"
        "    return None\n"
        "\n"
        "\n"
        "try:\n"
        "    _load()\n"
        "except Exception as record_ready_queue_depth:\n"
        "    pass\n"
    )
    assert (
        cross_file._importable_definition_spans_for_names(
            handler_rebind,
            frozenset({"record_ready_queue_depth"}),
            path=_CALLEE_MODULE,
        )
        == []
    )

    bare_target = handler_rebind.replace(
        "except Exception as record_ready_queue_depth:", "except Exception:"
    )
    assert cross_file._importable_definition_spans_for_names(
        bare_target,
        frozenset({"record_ready_queue_depth"}),
        path=_CALLEE_MODULE,
    ) == [(1, 4)]


@pytest.mark.unit
def test_a_function_local_exception_target_shadows_no_module_definition() -> None:
    """A handler target inside a helper binds that helper's local, not the module name.

    The same scope attribution every other binding form gets: ``def other():
    try: ... except Exception as record`` cannot reach what an importer of the
    module-level ``def record`` sees, so withholding that span would park a
    correct cross-file correction as ``needs_human``.
    """
    text = (
        "def record_ready_queue_depth(payload):\n"
        "    return None\n"
        "\n"
        "\n"
        "def other():\n"
        "    try:\n"
        "        _load()\n"
        "    except Exception as record_ready_queue_depth:\n"
        "        return record_ready_queue_depth\n"
    )

    assert cross_file._importable_definition_spans_for_names(
        text,
        frozenset({"record_ready_queue_depth"}),
        path=_CALLEE_MODULE,
    ) == [(1, 4)]


@pytest.mark.unit
def test_deleted_definitions_are_not_importable() -> None:
    """``del record`` leaves the ``def record`` above it importable from nowhere.

    A ``del`` statement unbinds the name outright, so a module body that runs
    it replaces nothing and leaves ``from module import record`` failing: the
    ``def`` above it is dead code no importer can reach. Reading only the
    binding forms that *rebind* a name still offered that head as the callee a
    correction has to touch, so an edit confined to it satisfied the cross-file
    evidence gate and resolved the thread while the import stayed broken
    (PRRT_kwDOSJAM6s6rB_UI). An attribute target such as ``del obj.record``
    unbinds no name of the scope and shadows nothing.
    """
    deleted = (
        "def record_ready_queue_depth(payload):\n"
        "    return None\n"
        "\n"
        "\n"
        "del record_ready_queue_depth\n"
    )
    assert (
        cross_file._importable_definition_spans_for_names(
            deleted,
            frozenset({"record_ready_queue_depth"}),
            path=_CALLEE_MODULE,
        )
        == []
    )

    attribute_target = deleted.replace(
        "del record_ready_queue_depth\n", "del _registry.record_ready_queue_depth\n"
    )
    assert cross_file._importable_definition_spans_for_names(
        attribute_target,
        frozenset({"record_ready_queue_depth"}),
        path=_CALLEE_MODULE,
    ) == [(1, 4)]


@pytest.mark.unit
def test_a_function_local_delete_shadows_no_module_definition() -> None:
    """``del`` inside a helper unbinds that helper's local, not the module name.

    The same scope attribution every other binding form gets, with the one
    exception every other form has too: a ``global record`` declaration makes
    the helper's ``del`` unbind the *module* name, so the dead head above it
    stays withheld.
    """
    local_delete = (
        "def record_ready_queue_depth(payload):\n"
        "    return None\n"
        "\n"
        "\n"
        "def other():\n"
        "    record_ready_queue_depth = _load()\n"
        "    del record_ready_queue_depth\n"
    )
    assert cross_file._importable_definition_spans_for_names(
        local_delete,
        frozenset({"record_ready_queue_depth"}),
        path=_CALLEE_MODULE,
    ) == [(1, 4)]

    global_delete = local_delete.replace(
        "    record_ready_queue_depth = _load()\n", "    global record_ready_queue_depth\n"
    )
    assert (
        cross_file._importable_definition_spans_for_names(
            global_delete,
            frozenset({"record_ready_queue_depth"}),
            path=_CALLEE_MODULE,
        )
        == []
    )
