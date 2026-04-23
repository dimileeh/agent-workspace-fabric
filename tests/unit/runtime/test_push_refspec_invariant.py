"""Codebase-wide invariant: never ``git push origin HEAD`` without an
explicit refspec.

The 2026-04-23 aira-web incident happened because
``pr_monitor_runner._git_push`` issued ``git push origin HEAD``.
That command's destination depends on ``push.default`` and
``branch.<current>.merge`` — both of which had been polluted on the
shared bare mirror by prior sync workspaces. The polluted config
redirected HEAD to ``development``, putting four feature-branch
commits on the shared base branch and bypassing review.

**The invariant**: every ``git push origin HEAD`` the codebase
issues MUST be followed immediately by a ``:refs/heads/...``
refspec suffix (either ``HEAD:refs/heads/<branch>`` as a single
argument, OR paired inside an args list). No bare ``"HEAD"`` arg
after ``"origin"``.

This test is a guardrail against future refactors that "simplify"
back to the ambiguous form. If you need to push the current
branch, pass the explicit refspec — nothing else is safe in the
multi-worktree, shared-mirror layout AWF uses.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).parents[3] / "src"
_SCRIPTS = Path(__file__).parents[3] / "scripts"


def _iter_py_files() -> list[Path]:
    return [p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts] + [
        p for p in _SCRIPTS.rglob("*.py") if "__pycache__" not in p.parts
    ]


def _find_push_list_literals(tree: ast.AST) -> list[tuple[int, list[ast.expr]]]:
    """Return (lineno, elts) for every list literal that starts with
    ``"git"``-ish and contains the sequence ``"push", "origin", ...``.
    We look at list literals because every git command in this codebase
    is built as a list passed to ``runner.run([...])``.
    """
    found: list[tuple[int, list[ast.expr]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List):
            continue
        elts = node.elts
        if len(elts) < 4:
            continue
        # Collect only string-constant literals; dynamic f-strings don't
        # concern this test because they can't produce bare "HEAD" via
        # a constant "origin" neighbour without making it ambiguous.
        first = elts[0]
        if not (isinstance(first, ast.Constant) and first.value == "git"):
            continue
        string_vals = [
            e.value for e in elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
        if "push" in string_vals and "origin" in string_vals:
            found.append((node.lineno, elts))
    return found


def _literal_strings(elts: list[ast.expr]) -> list[str | None]:
    """Map each element to its string value, or None if not a constant."""
    out: list[str | None] = []
    for e in elts:
        if isinstance(e, ast.Constant) and isinstance(e.value, str):
            out.append(e.value)
        else:
            out.append(None)
    return out


def test_no_bare_push_origin_head_in_codebase() -> None:  # noqa: N802 — historical name retained
    """No ``git push origin HEAD`` without an explicit refspec anywhere.

    Flags list literals where an element is exactly ``"HEAD"`` and the
    previous string element is ``"origin"``. The safe form is
    ``HEAD:refs/heads/<branch>`` as a single string, which this check
    allows (since the literal element would be
    ``"HEAD:refs/heads/..."`` — equal to the prefix ``"HEAD:"`` but not
    equal to just ``"HEAD"``).
    """
    offenders: list[str] = []
    for path in _iter_py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for lineno, elts in _find_push_list_literals(tree):
            vals = _literal_strings(elts)
            # Find positions of "origin" followed by bare "HEAD".
            for i in range(len(vals) - 1):
                if vals[i] == "origin" and vals[i + 1] == "HEAD":
                    offenders.append(
                        f"{path.relative_to(path.parents[3])}:{lineno} — bare"
                        " 'HEAD' after 'origin' (use 'HEAD:refs/heads/<branch>')"
                    )
    assert not offenders, (
        "Found bare ``git push origin HEAD`` without refspec — that's"
        " the 2026-04-23 aira-web regression. Offenders:\n  " + "\n  ".join(offenders)
    )


def test_monitor_git_push_arguments_carry_refspec() -> None:
    """Narrower assertion on the exact function that caused the
    incident: ``pr_monitor_runner._git_push``. The push command it
    issues must include a ``refs/heads/`` refspec."""
    from awf.runtime import pr_monitor_runner

    source = Path(pr_monitor_runner.__file__).read_text(encoding="utf-8")
    # Inside the _git_push body we expect the refspec string. Absence
    # would mean the fix got reverted.
    assert "HEAD:refs/heads/" in source, (
        "pr_monitor_runner must push via an explicit"
        " ``HEAD:refs/heads/<branch>`` refspec — reverting to"
        " ``push origin HEAD`` reopens the 2026-04-23 bug."
    )


@pytest.mark.unit
def test_no_push_default_upstream_writes_in_configure_helper() -> None:
    """Second layer of defense: make sure the helper that writes branch
    push config never writes ``push.default=upstream``. A per-workspace
    helper has no business touching a global config knob.

    We parse the function's AST and inspect the list literals it emits
    (the ``[f"branch.<X>.remote", "origin"]`` etc. tuples). The
    docstring is intentionally allowed to mention ``push.default`` —
    that's where we explain WHY we don't set it — so a plain substring
    check won't do."""
    import inspect

    from scripts.run_awf import _configure_branch_push_upstream

    src = inspect.getsource(_configure_branch_push_upstream)
    tree = ast.parse(src)
    func = tree.body[0]
    assert isinstance(func, ast.AsyncFunctionDef)

    # Strip the docstring so the literal-scan below only sees code.
    body = func.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]

    code_node = ast.Module(body=body, type_ignores=[])
    for node in ast.walk(code_node):
        if isinstance(node, ast.Constant) and node.value == "push.default":
            raise AssertionError(
                "_configure_branch_push_upstream writes push.default —"
                " that's the write that turned the 2026-04-23 monitor"
                " push into a development-branch push. Remove it."
            )
