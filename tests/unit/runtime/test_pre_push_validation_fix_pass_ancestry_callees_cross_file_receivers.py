"""Receiver-binding rules for cross-file callee evidence (issue #1019).

Unit tests for how a qualified callee's *receiver* narrows the cross-file
evidence gate: which changed path it can reach, and — here — which definitions
inside that path its binding kind allows. Kept beside
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
    _CALLER,
    _LEFT,
    _Probe,
    _probe,
)

# A module holding both a module-level callee and a same-named class method.
_SHADOWED_METHOD_TEXT = (
    "GAUGE = None\n"
    "\n"
    "\n"
    "class Collector:\n"
    "    def record_ready_queue_depth(self, payload):\n"
    "        return payload\n"
    "\n"
    "\n"
    "def record_ready_queue_depth(payload):\n"
    "    GAUGE.set(len(payload.entries))\n"
    "    return None\n"
)

# A body-only change inside ``Collector.record_ready_queue_depth`` (old line 6).
_METHOD_ONLY_DIFF = (
    f"--- a/{_CALLEE_MODULE}\n"
    f"+++ b/{_CALLEE_MODULE}\n"
    "@@ -6 +6 @@\n"
    "-        return payload\n"
    "+        return payload.ready_depth()\n"
)

# A body-only change inside the module-level callee (old line 10).
_MODULE_LEVEL_DIFF = (
    f"--- a/{_CALLEE_MODULE}\n"
    f"+++ b/{_CALLEE_MODULE}\n"
    "@@ -10 +10 @@\n"
    "-    GAUGE.set(len(payload.entries))\n"
    "+    GAUGE.set(payload.ready_depth())\n"
)

_PLAIN_IMPORT_CALLER_TEXT = (
    "import pkg_b.observability.execution_platform_metrics as metrics\n"
    "\n"
    "\n"
    "def refresh(payload):\n"
    "    metrics.record_ready_queue_depth(payload)\n"
)


def _plain_import_receiver_probe(*, diff: str) -> _Probe:
    return _Probe(
        texts={
            (_LEFT, _CALLER): _PLAIN_IMPORT_CALLER_TEXT,
            (_LEFT, _CALLEE_MODULE): _SHADOWED_METHOD_TEXT,
        },
        changed_paths=(_CALLEE_MODULE,),
        diffs={_CALLEE_MODULE: diff},
    )


@pytest.mark.unit
async def test_a_module_receiver_does_not_resolve_a_same_named_class_method() -> None:
    """``import M as m`` proves ``m`` is a module, so ``m.record()`` is module-level.

    The imported module holds both a module-level ``record_ready_queue_depth``
    and ``Collector.record_ready_queue_depth``. The method is not an attribute
    of the module, so editing only the method leaves the callee the call site
    actually reaches untouched — and the surviving module-level function would
    otherwise satisfy the survival check and resolve the thread
    (PRRT_kwDOSJAM6s6q8-M1).
    """
    assert not await _probe(_plain_import_receiver_probe(diff=_METHOD_ONLY_DIFF), item_line=5)


@pytest.mark.unit
async def test_a_module_receiver_still_resolves_the_module_level_definition() -> None:
    """The #1019 shape keeps resolving: the module-level callee is the attribute."""
    assert await _probe(_plain_import_receiver_probe(diff=_MODULE_LEVEL_DIFF), item_line=5)


@pytest.mark.unit
async def test_an_unreadable_receiver_keeps_the_class_member_tolerance() -> None:
    """A receiver no import binds may be an instance, so its method resolves.

    ``collector`` here is a parameter, which keeps the name-only rule the #1019
    fixes depend on; restricting *proven modules* must not narrow it.
    """
    caller = "def refresh(collector, payload):\n    collector.record_ready_queue_depth(payload)\n"
    probe = _Probe(
        texts={
            (_LEFT, _CALLER): caller,
            (_LEFT, _CALLEE_MODULE): _SHADOWED_METHOD_TEXT,
        },
        changed_paths=(_CALLEE_MODULE,),
        diffs={_CALLEE_MODULE: _METHOD_ONLY_DIFF},
    )

    assert await _probe(probe, item_line=2)


@pytest.mark.unit
def test_only_plain_imports_prove_a_receiver_is_a_module() -> None:
    """``import M [as m]`` binds a module; a ``from`` import may bind an object."""
    text = (
        "import pkg.obs\n"
        "import pkg.other as alt\n"
        "from pkg.obs import collector\n"
        "from pkg.obs import metrics as m\n"
    )
    assert cross_file._module_bound_receiver_names(text, path="src/pkg_a/caller.py") == frozenset(
        {"obs", "alt"}
    )
    # A name both forms bind is rebound, so it is not claimed as a module here
    # and fails closed on the ambiguous target instead.
    assert (
        cross_file._module_bound_receiver_names(
            "import pkg.obs as collector\nfrom pkg.obs import collector\n",
            path="src/pkg_a/caller.py",
        )
        == frozenset()
    )


# A caller shallow enough that ``from ..`` climbs past the repo root, so the
# import reader cannot resolve the module it rebinds ``metrics`` to.
_REBOUND_CALLER = "pkg_a/caller.py"
_REBOUND_CALLEE = "pkg_b/observability/execution_platform_metrics.py"

_REBOUND_CALLER_TEXT = (
    "import pkg_b.observability.execution_platform_metrics as metrics\n"
    "from .. import metrics\n"
    "\n"
    "\n"
    "def refresh(payload):\n"
    "    metrics.record_ready_queue_depth(payload)\n"
)

_PROVEN_CALLER_TEXT = (
    "import pkg_b.observability.execution_platform_metrics as metrics\n"
    "\n"
    "\n"
    "def refresh(payload):\n"
    "    metrics.record_ready_queue_depth(payload)\n"
)

# A body-only change inside the module-level callee of ``_SHADOWED_METHOD_TEXT``.
_REBOUND_CALLEE_DIFF = (
    f"--- a/{_REBOUND_CALLEE}\n"
    f"+++ b/{_REBOUND_CALLEE}\n"
    "@@ -10 +10 @@\n"
    "-    GAUGE.set(len(payload.entries))\n"
    "+    GAUGE.set(payload.ready_depth())\n"
)


def _rebound_receiver_probe(*, caller_text: str) -> _Probe:
    return _Probe(
        texts={
            (_LEFT, _REBOUND_CALLER): caller_text,
            (_LEFT, _REBOUND_CALLEE): _SHADOWED_METHOD_TEXT,
        },
        changed_paths=(_REBOUND_CALLEE,),
        diffs={_REBOUND_CALLEE: _REBOUND_CALLEE_DIFF},
    )


@pytest.mark.unit
async def test_an_unresolvable_relative_import_unproves_a_module_receiver() -> None:
    """A ``from`` import the reader cannot resolve still rebinds the receiver.

    ``from .. import metrics`` here climbs past the repo root, so no module path
    comes out of it — but it rebinds ``metrics`` all the same, which leaves the
    plain ``import`` no longer proof that the receiver is that module. Counting
    the unresolvable statement as one of the name's identities is what makes the
    receiver fail closed instead of resolving against the plain import's module
    (PRRT_kwDOSJAM6s6q8-M1).
    """
    probe = _rebound_receiver_probe(caller_text=_REBOUND_CALLER_TEXT)

    assert not await _probe(probe, item_path=_REBOUND_CALLER, item_line=6)


@pytest.mark.unit
async def test_the_same_receiver_resolves_when_nothing_rebinds_it() -> None:
    """The paired accept: without the rebinding the module receiver still resolves."""
    probe = _rebound_receiver_probe(caller_text=_PROVEN_CALLER_TEXT)

    assert await _probe(probe, item_path=_REBOUND_CALLER, item_line=5)


@pytest.mark.unit
def test_an_unresolvable_import_counts_as_a_rebinding_of_the_name() -> None:
    """The receiver is neither a proven module nor bound to the plain import."""
    assert (
        cross_file._module_bound_receiver_names(_REBOUND_CALLER_TEXT, path=_REBOUND_CALLER)
        == frozenset()
    )
    assert (
        cross_file._receiver_import_module_targets(_REBOUND_CALLER_TEXT, path=_REBOUND_CALLER)[
            "metrics"
        ]
        == cross_file._AMBIGUOUS_IMPORT_TARGET
    )


# The two import forms whose *paths* coincide: ``import pkg.mod as metrics``
# binds the submodule, while ``from pkg import mod as metrics`` binds whatever
# ``pkg`` exposes under that name. Both describe ``pkg/mod``, so a rebinding
# guard keyed on the path alone cannot see the second statement.
_DUAL_FORM_CALLER_TEXT = (
    "import pkg_b.observability.execution_platform_metrics as metrics\n"
    "from pkg_b.observability import execution_platform_metrics as metrics\n"
    "\n"
    "\n"
    "def refresh(payload):\n"
    "    metrics.record_ready_queue_depth(payload)\n"
)


@pytest.mark.unit
async def test_a_from_import_of_the_same_path_unproves_a_module_receiver() -> None:
    """``import pkg.mod`` plus ``from pkg import mod`` rebinds the receiver.

    The two statements bind ``metrics`` to different objects even though their
    module paths are the same string, so the plain ``import`` is no longer proof
    that the receiver is that module — and the class-member tolerance
    ``_module_bound_receiver_names`` drops for a rebound name must not come back
    through a guard that reads the pair as one repeated import. Otherwise a
    correction to ``Collector.record_ready_queue_depth`` alone satisfies this
    gate for a call site whose callee may never have been a class member
    (PRRT_kwDOSJAM6s6q9Xo3).
    """
    probe = _plain_import_receiver_probe(diff=_METHOD_ONLY_DIFF)
    probe.texts[(_LEFT, _CALLER)] = _DUAL_FORM_CALLER_TEXT

    assert not await _probe(probe, item_line=6)


@pytest.mark.unit
def test_the_two_import_forms_are_distinct_bindings_of_one_path() -> None:
    """The receiver is neither a proven module nor bound to either import."""
    assert (
        cross_file._module_bound_receiver_names(_DUAL_FORM_CALLER_TEXT, path=_CALLER) == frozenset()
    )
    assert (
        cross_file._receiver_import_module_targets(_DUAL_FORM_CALLER_TEXT, path=_CALLER)["metrics"]
        == cross_file._AMBIGUOUS_IMPORT_TARGET
    )
