"""Import-head spellings the cross-file callee-evidence reader has to match.

Python does not require whitespace after the ``import`` keyword when a
parenthesized target list follows, so ``from pkg import(record)`` is a real
import head. These tests pin the reader on that spelling; they live in their own
module because the main cross-file test module sits at the first-party file line
limit enforced by ``tests/unit/test_core_decomposition_maintainability.py``.
"""

from __future__ import annotations

import pytest

from awf.runtime.pr_monitor_runner import (
    pre_push_validation_fix_pass_ancestry_cross_file as cross_file,
)
from tests.unit.runtime._pre_push_ancestry_cross_file_helpers import (
    _CALLEE_TEXT,
    _CALLER,
    _IN_SPAN_DIFF,
    _LEFT,
    _Probe,
    _probe,
)

_UNRELATED_SAME_NAME = "src/pkg_c/unrelated.py"


def _unrelated_same_name_probe(*, caller_text: str) -> _Probe:
    """A commit range that changed a same-named def in a module nobody imported."""
    return _Probe(
        texts={
            (_LEFT, _CALLER): caller_text,
            (_LEFT, _UNRELATED_SAME_NAME): _CALLEE_TEXT,
        },
        changed_paths=(_UNRELATED_SAME_NAME,),
        diffs={
            _UNRELATED_SAME_NAME: _IN_SPAN_DIFF.replace(
                "src/pkg_b/observability/execution_platform_metrics.py", _UNRELATED_SAME_NAME
            )
        },
    )


@pytest.mark.unit
def test_from_import_heads_bind_without_post_keyword_whitespace() -> None:
    """``from M import(name)`` is valid Python, so its names bind to ``M``.

    The target list is the only bracket an import head can open, and ``(`` ends
    the keyword as surely as a space does. Held to whitespace, the head matches
    nothing, the name keeps the name-only rule, and a correction to an unrelated
    same-named definition in another package resolves the thread
    (PRRT_kwDOSJAM6s6rBbdr). Both the absolute and the relative form are read,
    wrapped as well as on one line.
    """
    assert cross_file._bare_name_import_module_paths(
        "from pkg.mod import(record)\nfrom pkg.other import(\n    alpha,\n    beta as gamma,\n)\n",
        path="src/pkg/caller.py",
    ) == {
        "record": frozenset({"pkg/mod"}),
        "alpha": frozenset({"pkg/other"}),
        "gamma": frozenset({"pkg/other"}),
    }
    assert cross_file._bare_name_import_module_paths(
        "from .mod import(delta)\nfrom ..up import(epsilon)\n",
        path="src/pkg/caller.py",
    ) == {
        "delta": frozenset({"src/pkg/mod"}),
        "epsilon": frozenset({"src/up"}),
    }
    # A keyword followed by anything else is still not an import head.
    assert (
        cross_file._bare_name_import_module_paths(
            "from pkg.mod importrecord\n", path="src/pkg/caller.py"
        )
        == {}
    )


@pytest.mark.unit
async def test_callee_imported_without_post_keyword_whitespace_fails_closed() -> None:
    """The bound name reaches only its own module, however the head is spelled."""
    caller = (
        "from pkg_b.observability.execution_platform_metrics import(record_ready_queue_depth)\n"
        "\n"
        "\n"
        "def refresh(payload):\n"
        "    record_ready_queue_depth(payload)\n"
    )

    assert not await _probe(_unrelated_same_name_probe(caller_text=caller), item_line=5)
