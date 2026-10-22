"""Import bindings that narrow the cross-file callee candidate (issue #1019).

Unit tests for the half of the fourth evidence gate that decides *which*
changed file may hold the callee: the call site's own ``from`` / plain
``import`` statements bind each bare callee and each receiver to a module path,
and a same-named definition outside it is not evidence about the call
(PRRT_kwDOSJAM6s6q7bSI). Split off
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
    _CALLER_TEXT,
    _IN_SPAN_DIFF,
    _LEFT,
    _cross_package_probe,
    _Probe,
    _probe,
)

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
def test_semicolon_separated_statements_are_split_before_the_names_are_read() -> None:
    """Each simple statement on a logical line is matched as its own head.

    ``from pkg.mod import record; cache = {}`` is one logical line holding two
    simple statements. Read whole, the target list is ``record; cache = {}``,
    which binds ``record;`` and leaves ``record`` on the name-only rule that
    accepts an unrelated same-named definition in another package
    (PRRT_kwDOSJAM6s6rBKOW). A plain import has the same shape, and an import
    that *follows* the separator is a head of its own.
    """
    assert cross_file._bare_name_import_module_paths(
        "from pkg.mod import record; cache = {}\ncache_other = {}; from pkg.other import replay\n",
        path="src/pkg/caller.py",
    ) == {
        "record": frozenset({"pkg/mod"}),
        "replay": frozenset({"pkg/other"}),
    }
    assert cross_file._plain_import_module_paths(
        "import pkg.mod; cache = {}\nimport a.b; import c.d\n",
        path="src/pkg/caller.py",
    ) == {
        "mod": frozenset({"pkg/mod"}),
        "pkg": frozenset({"pkg"}),
        "b": frozenset({"a/b"}),
        "a": frozenset({"a"}),
        "d": frozenset({"c/d"}),
        "c": frozenset({"c"}),
    }


@pytest.mark.unit
def test_statements_after_a_wrapped_target_list_are_split_off_too() -> None:
    """A head that follows a parenthesized target list is a head of its own.

    The statement splitter runs before the bracketed continuation is joined, so
    ``from pkg.real import (`` + ``record`` + ``); from pkg.decoy import record``
    leaves the second import embedded in the first one's targets. Read whole, the
    rebinding stays invisible: the name keeps ``pkg/real`` while the call site
    actually reaches ``pkg.decoy``, so a correction to the shadowed definition
    would resolve the thread (PRRT_kwDOSJAM6s6rBTTM). Split off, both bindings
    are seen and the rebound name fails closed. A trailing statement that is not
    an import still binds nothing of its own.
    """
    decoy = "from pkg.real import (\n    record\n); from pkg.decoy import record\n"
    assert cross_file._bare_name_import_module_paths(decoy, path="src/pkg/caller.py") == {
        "record": frozenset({"pkg/real", "pkg/decoy"})
    }
    assert cross_file._bare_name_import_module_targets(decoy, path="src/pkg/caller.py") == {
        "record": cross_file._AMBIGUOUS_IMPORT_TARGET
    }
    # The trailing head may wrap its own target list; it is balanced by the join.
    wrapped = "from pkg.real import (\n    record,\n); from pkg.decoy import (\n    record\n)\n"
    assert cross_file._bare_name_import_module_targets(wrapped, path="src/pkg/caller.py") == {
        "record": cross_file._AMBIGUOUS_IMPORT_TARGET
    }
    plain_suffix = "from pkg.real import (\n    record,\n); cache = {}\n"
    assert cross_file._bare_name_import_module_paths(plain_suffix, path="src/pkg/caller.py") == {
        "record": frozenset({"pkg/real"})
    }


@pytest.mark.unit
def test_plain_import_after_a_wrapped_target_list_is_read_as_its_own_head() -> None:
    """The receiver reader has to split that trailing head off as well.

    ``from pkg.x import (`` + ``y,`` + ``); import pkg.decoy as metrics`` leaves
    the plain import on the closing line of the wrapped list, where the bracket
    depth gate hides it. Unread, ``metrics`` keeps the *shadowed* ``pkg/real``
    binding while the call site reaches ``pkg.decoy``, so a correction to
    ``pkg.real``'s definition would satisfy the gate (PRRT_kwDOSJAM6s6rBTTM).
    Split off, the rebinding is seen and the receiver fails closed.
    """
    decoy = (
        "import pkg.real as metrics\nfrom pkg.x import (\n    y,\n); import pkg.decoy as metrics\n"
    )
    assert cross_file._plain_import_module_paths(decoy, path="src/pkg/caller.py") == {
        "metrics": frozenset({"pkg/real", "pkg/decoy"})
    }
    assert (
        cross_file._receiver_import_module_targets(decoy, path="src/pkg/caller.py")["metrics"]
        == cross_file._AMBIGUOUS_IMPORT_TARGET
    )
    # A non-import suffix still binds no receiver of its own.
    assert (
        cross_file._plain_import_module_paths(
            "from pkg.x import (\n    y,\n); cache = {}\n", path="src/pkg/caller.py"
        )
        == {}
    )


@pytest.mark.unit
def test_plain_import_bindings_skip_pieces_that_are_not_module_names() -> None:
    """Only dotted module names bind; the trailing-comma/garbage pieces do not."""
    assert cross_file._plain_import_module_paths(
        "import a.b  # note\nimport a.b as c\nimport (\n", path="m.py"
    ) == {"b": frozenset({"a/b"}), "c": frozenset({"a/b"}), "a": frozenset({"a"})}
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
async def test_a_backslash_continued_import_still_binds_its_targets() -> None:
    """``from M import \\`` + ``name`` is one statement, so the name binds to ``M``.

    Left unjoined the head binds the backslash instead, ``record_ready_queue_depth``
    keeps the name-only rule, and editing an unrelated same-named def in ``pkg_c``
    would resolve the thread (PRRT_kwDOSJAM6s6rAf0X).
    """
    caller = (
        "from pkg_b.observability.execution_platform_metrics import \\\n"
        "    record_ready_queue_depth\n"
        "\n"
        "\n"
        "def refresh(payload):\n"
        "    record_ready_queue_depth(payload)\n"
    )

    assert not await _probe(_unrelated_same_name_probe(caller_text=caller), item_line=6)


@pytest.mark.unit
def test_backslash_continuations_are_joined_before_the_names_are_read() -> None:
    """The marker is consumed on the head, before ``import``, and inside a block."""
    assert cross_file._bare_name_import_module_paths(
        "from pkg.mod import \\\n"
        "    alpha, beta as gamma\n"
        "from pkg.other \\\n"
        "    import delta\n"
        "from pkg.third import (\n"
        "    epsilon, \\\n"
        "    zeta,\n"
        ")\n",
        path="src/pkg/caller.py",
    ) == {
        "alpha": frozenset({"pkg/mod"}),
        "gamma": frozenset({"pkg/mod"}),
        "delta": frozenset({"pkg/other"}),
        "epsilon": frozenset({"pkg/third"}),
        "zeta": frozenset({"pkg/third"}),
    }


@pytest.mark.unit
def test_a_continuation_with_no_following_line_binds_nothing() -> None:
    """An unterminated continuation has no target list, so it fails closed."""
    assert (
        cross_file._bare_name_import_module_paths(
            "from pkg.mod import \\\n", path="src/pkg/caller.py"
        )
        == {}
    )


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
