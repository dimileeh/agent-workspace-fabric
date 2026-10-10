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
    _CALLEE_TEXT,
    _CALLER,
    _IN_SPAN_DIFF,
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


# The importing module's own file, holding ``record_ready_queue_depth`` on two
# module-level classes: the one the caller imports as a receiver, and an
# unrelated neighbour that happens to expose the same method name.
_IMPORTED_OBJECT_MODULE = "src/pkg_b/observability.py"

_TWO_CLASS_TEXT = (
    "class Collector:\n"
    "    def record_ready_queue_depth(self, payload):\n"
    "        return payload\n"
    "\n"
    "\n"
    "class Other:\n"
    "    def record_ready_queue_depth(self, payload):\n"
    "        return payload\n"
)

# A body-only change inside ``Collector.record_ready_queue_depth`` (old line 3).
_COLLECTOR_METHOD_DIFF = (
    f"--- a/{_IMPORTED_OBJECT_MODULE}\n"
    f"+++ b/{_IMPORTED_OBJECT_MODULE}\n"
    "@@ -3 +3 @@\n"
    "-        return payload\n"
    "+        return payload.ready_depth()\n"
)

# The same edit against ``Other.record_ready_queue_depth`` (old line 8).
_OTHER_METHOD_DIFF = (
    f"--- a/{_IMPORTED_OBJECT_MODULE}\n"
    f"+++ b/{_IMPORTED_OBJECT_MODULE}\n"
    "@@ -8 +8 @@\n"
    "-        return payload\n"
    "+        return payload.ready_depth()\n"
)

_IMPORTED_CLASS_CALLER_TEXT = (
    "from pkg_b.observability import Collector\n"
    "\n"
    "\n"
    "def refresh(payload):\n"
    "    Collector.record_ready_queue_depth(payload)\n"
)


def _imported_object_receiver_probe(*, diff: str, caller_text: str) -> _Probe:
    return _Probe(
        texts={
            (_LEFT, _CALLER): caller_text,
            (_LEFT, _IMPORTED_OBJECT_MODULE): _TWO_CLASS_TEXT,
        },
        changed_paths=(_IMPORTED_OBJECT_MODULE,),
        diffs={_IMPORTED_OBJECT_MODULE: diff},
    )


@pytest.mark.unit
async def test_an_imported_object_receiver_rejects_another_class_s_method() -> None:
    """``from M import Collector`` binds ``Collector``, not every class in ``M``.

    The importing module's own file satisfies the receiver's exact target, but
    ``Collector.record_ready_queue_depth()`` reaches ``Collector``'s member
    alone: editing ``Other.record_ready_queue_depth`` leaves the call site's
    callee untouched, and the surviving ``Collector`` method would otherwise
    carry the survival check too (PRRT_kwDOSJAM6s6q-L4H).
    """
    probe = _imported_object_receiver_probe(
        diff=_OTHER_METHOD_DIFF, caller_text=_IMPORTED_CLASS_CALLER_TEXT
    )

    assert not await _probe(probe, item_line=5)


@pytest.mark.unit
async def test_an_imported_object_receiver_resolves_its_own_method() -> None:
    """The paired accept: the imported symbol's own member is still evidence."""
    probe = _imported_object_receiver_probe(
        diff=_COLLECTOR_METHOD_DIFF, caller_text=_IMPORTED_CLASS_CALLER_TEXT
    )

    assert await _probe(probe, item_line=5)


# ``Collector`` declared under a module-level ``if``: textually indented, yet
# still bound in the module globals the import reads.
_GUARDED_CLASS_TEXT = (
    "import sys\n"
    "\n"
    "if sys.version_info >= (3, 12):\n"
    "\n"
    "    class Collector:\n"
    "        def record_ready_queue_depth(self, payload):\n"
    "            return payload\n"
)

# A body-only change inside the guarded ``Collector`` method (old line 7).
_GUARDED_METHOD_DIFF = (
    f"--- a/{_IMPORTED_OBJECT_MODULE}\n"
    f"+++ b/{_IMPORTED_OBJECT_MODULE}\n"
    "@@ -7 +7 @@\n"
    "-            return payload\n"
    "+            return payload.ready_depth()\n"
)


@pytest.mark.unit
async def test_an_imported_object_receiver_resolves_an_if_guarded_class_s_method() -> None:
    """A class under a module-level ``if`` still binds in the module globals.

    ``from M import Collector`` reaches the class the executed branch bound, so
    holding the pinned scope to a *textual* indent of 0 would drop a real
    ``Collector.record_ready_queue_depth`` correction and park the item as
    needs_human (PRRT_kwDOSJAM6s6q-8T_). Nothing else encloses the class, which
    is what makes it module-scoped.
    """
    probe = _imported_object_receiver_probe(
        diff=_GUARDED_METHOD_DIFF, caller_text=_IMPORTED_CLASS_CALLER_TEXT
    )
    probe.texts[(_LEFT, _IMPORTED_OBJECT_MODULE)] = _GUARDED_CLASS_TEXT

    assert await _probe(probe, item_line=5)


@pytest.mark.unit
async def test_an_imported_object_receiver_rejects_a_module_level_definition() -> None:
    """A module-level ``def`` is no attribute of the object the import bound.

    The exact target exists only for the reading where the receiver is an object
    ``pkg_b/observability.py`` itself defines, so a module-level helper of the
    same name there is not what ``Collector.record_ready_queue_depth()`` calls
    (PRRT_kwDOSJAM6s6q-L4H). The submodule reading keeps its own target and is
    unaffected.
    """
    module_level = "def record_ready_queue_depth(payload):\n    return payload\n\n\nclass Collector:\n    pass\n"
    probe = _imported_object_receiver_probe(
        diff=(
            f"--- a/{_IMPORTED_OBJECT_MODULE}\n"
            f"+++ b/{_IMPORTED_OBJECT_MODULE}\n"
            "@@ -2 +2 @@\n"
            "-    return payload\n"
            "+    return payload.ready_depth()\n"
        ),
        caller_text=_IMPORTED_CLASS_CALLER_TEXT,
    )
    probe.texts[(_LEFT, _IMPORTED_OBJECT_MODULE)] = module_level

    assert not await _probe(probe, item_line=5)


@pytest.mark.unit
async def test_an_unbound_receiver_keeps_reaching_any_class_member() -> None:
    """A receiver no import binds keeps the name-only rule across both classes."""
    probe = _imported_object_receiver_probe(
        diff=_OTHER_METHOD_DIFF,
        caller_text="def refresh(Collector, payload):\n    Collector.record_ready_queue_depth(payload)\n",
    )

    assert await _probe(probe, item_line=2)


@pytest.mark.unit
def test_an_exact_receiver_target_carries_the_imported_symbol() -> None:
    """The symbol the span has to sit under rides along with the exact target."""
    assert cross_file._receiver_import_module_targets(
        "from pkg.obs import Collector\n", path=_CALLER
    ) == {
        "Collector": frozenset({("pkg/obs/Collector", False, None), ("pkg/obs", True, "Collector")})
    }


# A caller reaching its callee through a *chain* of receivers: the import binds
# ``obs``, and ``execution_platform_metrics`` is an attribute of that module —
# a name no import of this file binds.
_CHAINED_RECEIVER_CALLER_TEXT = (
    "import pkg_b.observability as obs\n"
    "\n"
    "\n"
    "def refresh(payload):\n"
    "    obs.execution_platform_metrics.record_ready_queue_depth(payload)\n"
)
_UNREACHABLE_SAME_NAME_MODULE = "src/pkg_c/unrelated.py"


def _chained_receiver_probe(*, caller_text: str, changed: str = _CALLEE_MODULE) -> _Probe:
    return _Probe(
        texts={(_LEFT, _CALLER): caller_text, (_LEFT, changed): _CALLEE_TEXT},
        changed_paths=(changed,),
        diffs={changed: _IN_SPAN_DIFF.replace(_CALLEE_MODULE, changed)},
    )


@pytest.mark.unit
async def test_a_chained_receiver_resolves_under_its_imported_root() -> None:
    """``obs.metrics.record()`` reaches the module its chain root's import names."""
    probe = _chained_receiver_probe(caller_text=_CHAINED_RECEIVER_CALLER_TEXT)

    assert await _probe(probe, item_line=5)


@pytest.mark.unit
async def test_a_chained_receiver_rejects_a_module_its_root_cannot_reach() -> None:
    """The callee's immediate qualifier is an attribute, so the root is the binding.

    Keying the ref on ``execution_platform_metrics`` — the only qualifier the
    anchored line's scan reports — leaves it bound to nothing, which would hand
    the callee the name-only rule and let a correction to any reachable
    ``record_ready_queue_depth`` resolve the thread while
    ``obs.execution_platform_metrics.record_ready_queue_depth`` stayed
    untouched (PRRT_kwDOSJAM6s6q-6LK).
    """
    probe = _chained_receiver_probe(
        caller_text=_CHAINED_RECEIVER_CALLER_TEXT, changed=_UNREACHABLE_SAME_NAME_MODULE
    )

    assert not await _probe(probe, item_line=5)


@pytest.mark.unit
async def test_a_chained_receiver_whose_root_no_import_binds_fails_closed() -> None:
    """An attribute chain is not a name an import binds, so it never falls back.

    The name-only tolerance a single unreadable receiver keeps cannot apply
    here: the receiver is an attribute of ``ctx``, which no import associates
    with the changed module, so the gate holds the callee closed instead of
    accepting a same-named definition (PRRT_kwDOSJAM6s6q-6LK).
    """
    probe = _chained_receiver_probe(
        caller_text=(
            "def refresh(ctx, payload):\n"
            "    ctx.execution_platform_metrics.record_ready_queue_depth(payload)\n"
        )
    )

    assert not await _probe(probe, item_line=2)


@pytest.mark.unit
async def test_a_receiver_chain_this_reader_cannot_resolve_fails_closed() -> None:
    """A chain rooted in a call expression leaves no name to bind, so it closes."""
    probe = _chained_receiver_probe(
        caller_text=(
            "import pkg_b.observability as obs\n"
            "\n"
            "\n"
            "def refresh(payload):\n"
            "    factory().execution_platform_metrics.record_ready_queue_depth(payload)\n"
        )
    )

    assert not await _probe(probe, item_line=5)


# A sibling of the chain's own module, under the same imported root.
_SIBLING_SAME_NAME_MODULE = "src/pkg_b/observability/unrelated.py"


@pytest.mark.unit
async def test_a_chained_receiver_rejects_a_sibling_under_its_root() -> None:
    """The chain's own segment is part of the binding, not just its root.

    ``import pkg_b.observability as obs`` plus
    ``obs.execution_platform_metrics.record_ready_queue_depth()`` reaches the
    ``execution_platform_metrics`` submodule, so binding the call to the whole
    ``pkg_b/observability`` subtree would let a correction to a same-named
    module-level helper in a *sibling* module satisfy the gate while the callee
    stayed untouched (PRRT_kwDOSJAM6s6q_M0j).
    """
    probe = _chained_receiver_probe(
        caller_text=_CHAINED_RECEIVER_CALLER_TEXT, changed=_SIBLING_SAME_NAME_MODULE
    )

    assert not await _probe(probe, item_line=5)


@pytest.mark.unit
async def test_an_unreadable_chain_fails_closed_even_when_its_qualifier_binds() -> None:
    """A chain this reader cannot read resolves to no module, so it closes.

    ``factory().execution_platform_metrics`` is an attribute of an unknown
    object, not the imported submodule of the same name, so the import's own
    target is no proof about this call (PRRT_kwDOSJAM6s6q_M0j).
    """
    probe = _chained_receiver_probe(
        caller_text=(
            "from pkg_b.observability import execution_platform_metrics\n"
            "\n"
            "\n"
            "def refresh(payload):\n"
            "    factory().execution_platform_metrics.record_ready_queue_depth(payload)\n"
        )
    )

    assert not await _probe(probe, item_line=5)


@pytest.mark.unit
async def test_two_chains_sharing_one_root_fail_closed() -> None:
    """One root reached through two different chains resolves to neither module.

    The root is the only binding key the resolver has, so a line spelling both
    ``obs.execution_platform_metrics.record_ready_queue_depth`` and
    ``obs.other.helper`` cannot hold the two callees to their own modules —
    keeping one of the chains would bind a callee to a module it never reaches
    (PRRT_kwDOSJAM6s6q_M0j).
    """
    probe = _chained_receiver_probe(
        caller_text=(
            "import pkg_b.observability as obs\n"
            "\n"
            "\n"
            "def refresh(payload):\n"
            "    obs.execution_platform_metrics.record_ready_queue_depth(obs.other.helper())\n"
        )
    )

    assert not await _probe(probe, item_line=5)


@pytest.mark.unit
async def test_a_chain_under_an_imported_package_resolves_its_own_submodule() -> None:
    """A ``from`` import's submodule reading carries the chain's segment too.

    ``from pkg_b import observability`` reads the root two ways; only the
    submodule one names a module, and the chain extends *that* path to
    ``pkg_b/observability/execution_platform_metrics``, which is the module the
    call actually reaches (PRRT_kwDOSJAM6s6q_M0j).
    """
    probe = _chained_receiver_probe(
        caller_text=(
            "from pkg_b import observability\n"
            "\n"
            "\n"
            "def refresh(payload):\n"
            "    observability.execution_platform_metrics.record_ready_queue_depth(payload)\n"
        )
    )

    assert await _probe(probe, item_line=5)


@pytest.mark.unit
async def test_a_chain_whose_root_the_anchored_scope_rebinds_fails_closed() -> None:
    """A rebound root names no module, so its chain extends nothing."""
    probe = _chained_receiver_probe(
        caller_text=(
            "import pkg_b.observability as obs\n"
            "\n"
            "\n"
            "def refresh(obs, payload):\n"
            "    obs.execution_platform_metrics.record_ready_queue_depth(payload)\n"
        )
    )

    assert not await _probe(probe, item_line=5)
