"""Augmented and type-alias rebindings of a cross-file callee (issue #1019).

Unit tests for the binding forms that replace a module's definition without
spelling a plain assignment: ``record += replacement`` and PEP 695's ``type
record = ...``. Both bind the name while carrying no definition head of their
own, so the ``def``/``class`` above them is dead code no importer reaches
(PRRT_kwDOSJAM6s6rCGKj). Kept beside
``test_pre_push_validation_fix_pass_ancestry_callees_cross_file_importability.py``
so both stay under the first-party file line limit.
"""

from __future__ import annotations

import pytest

from awf.runtime.pr_monitor_runner import (
    pre_push_validation_fix_pass_ancestry_cross_file as cross_file,
)
from tests.unit.runtime._pre_push_ancestry_cross_file_helpers import _CALLEE_MODULE

_AUGMENTED_REBIND = (
    "class record_ready_queue_depth:\n"
    "    def __init__(self, payload):\n"
    "        self.payload = payload\n"
    "\n"
    "\n"
    "record_ready_queue_depth += _extra_handler\n"
)


@pytest.mark.unit
def test_an_augmented_assignment_rebinding_is_not_importable() -> None:
    """``record += replacement`` rebinds the name, leaving the head above it dead.

    An augmented assignment stores back into the name, so an overload-capable
    candidate replaces its ``class record`` with whatever the operator returns
    and an importer reaches that object instead. Reading only the plain
    assignment forms still offered the dead head as the callee a correction has
    to touch, so an edit confined to it satisfied the cross-file evidence gate
    (PRRT_kwDOSJAM6s6rCGKj). An attribute target rebinds no name of the scope.
    """
    assert (
        cross_file._importable_definition_spans_for_names(
            _AUGMENTED_REBIND,
            frozenset({"record_ready_queue_depth"}),
            path=_CALLEE_MODULE,
        )
        == []
    )

    attribute_target = _AUGMENTED_REBIND.replace(
        "record_ready_queue_depth += _extra_handler",
        "_registry.record_ready_queue_depth += _extra_handler",
    )
    assert cross_file._importable_definition_spans_for_names(
        attribute_target,
        frozenset({"record_ready_queue_depth"}),
        path=_CALLEE_MODULE,
    ) == [(1, 5)]


@pytest.mark.unit
def test_a_type_alias_rebinding_is_not_importable() -> None:
    """``type record = ...`` binds the name to an alias object, not the head above it.

    A PEP 695 statement carries its name on the statement rather than on a
    definition head, so the earlier ``def record`` kept looking like the name's
    effective binding while importers receive the alias
    (PRRT_kwDOSJAM6s6rCGKj). An alias of some other name shadows nothing.
    """
    alias_rebind = (
        "def record_ready_queue_depth(payload):\n"
        "    return None\n"
        "\n"
        "\n"
        "type record_ready_queue_depth = int\n"
    )
    assert (
        cross_file._importable_definition_spans_for_names(
            alias_rebind,
            frozenset({"record_ready_queue_depth"}),
            path=_CALLEE_MODULE,
        )
        == []
    )

    other_name = alias_rebind.replace(
        "type record_ready_queue_depth = int", "type _QueueDepth = int"
    )
    assert cross_file._importable_definition_spans_for_names(
        other_name,
        frozenset({"record_ready_queue_depth"}),
        path=_CALLEE_MODULE,
    ) == [(1, 4)]


@pytest.mark.unit
def test_a_function_local_augmented_assignment_shadows_no_module_definition() -> None:
    """``record += ...`` inside a helper rebinds that helper's local only.

    The same scope attribution every other binding form gets: withholding the
    live module-level head for a local accumulator would park a correct
    cross-file correction as ``needs_human``.
    """
    text = (
        "def record_ready_queue_depth(payload):\n"
        "    return None\n"
        "\n"
        "\n"
        "def other(entries):\n"
        "    record_ready_queue_depth = 0\n"
        "    record_ready_queue_depth += len(entries)\n"
        "    return record_ready_queue_depth\n"
    )

    assert cross_file._importable_definition_spans_for_names(
        text,
        frozenset({"record_ready_queue_depth"}),
        path=_CALLEE_MODULE,
    ) == [(1, 4)]


@pytest.mark.unit
def test_an_augmented_rebinding_is_read_off_a_header_line_too() -> None:
    """``if enabled: record += extra`` rebinds the name past its line's left edge.

    The two placements no anchored reader sees — a suite written on its
    header's own line and a semicolon-separated statement — are read for an
    augmented assignment the same way they are for a plain one, because the
    ``ast`` walk sees the statement wherever Python spells it
    (PRRT_kwDOSJAM6s6rCGKj).
    """
    for rebind in (
        "if enabled: record_ready_queue_depth += _extra_handler\n",
        "flag = True; record_ready_queue_depth += _extra_handler\n",
    ):
        text = "def record_ready_queue_depth(payload):\n    return None\n\n\n" + rebind
        assert (
            cross_file._importable_definition_spans_for_names(
                text,
                frozenset({"record_ready_queue_depth"}),
                path=_CALLEE_MODULE,
            )
            == []
        )


@pytest.mark.unit
def test_a_parameterized_type_alias_rebinds_only_its_own_scope() -> None:
    """``type record[T] = list[T]`` rebinds; a class body's alias does not.

    A PEP 695 statement carrying type parameters binds its name exactly as the
    bare form does, so the ``def record`` above it is dead
    (PRRT_kwDOSJAM6s6rCGKj). The same scope attribution keeps a class body's
    alias — which binds in the class namespace, not the module's — from
    withholding a module-level head importers really reach.
    """
    assert (
        cross_file._importable_definition_spans_for_names(
            "def record_ready_queue_depth(payload):\n"
            "    return None\n"
            "\n"
            "\n"
            "type record_ready_queue_depth[T] = list[T]\n",
            frozenset({"record_ready_queue_depth"}),
            path=_CALLEE_MODULE,
        )
        == []
    )

    assert cross_file._importable_definition_spans_for_names(
        "def record_ready_queue_depth(payload):\n"
        "    return None\n"
        "\n"
        "\n"
        "class Holder:\n"
        "    type record_ready_queue_depth = int\n",
        frozenset({"record_ready_queue_depth"}),
        path=_CALLEE_MODULE,
    ) == [(1, 4)]
